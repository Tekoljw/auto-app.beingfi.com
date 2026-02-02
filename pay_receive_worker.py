#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代收代付后台常驻脚本
- 从 getAutoDataWithApp 拉取任务（云手机ID、收款APP列表、付款列表）
- 通过 VMOS 获取 ADB 连接并操作云手机
- 先执行代收：按 receive 查询各 APP 收款记录，提交 orderQueren
- 再执行代付：按 payment 执行付款，提交 paySuccessNotify
- 每轮结束后间隔 1 秒循环
可后台长期独立运行（建议用 nohup 或 Windows 计划任务）。

后台运行示例：
  Linux/Mac: nohup python pay_receive_worker.py --interval 1 > pay_receive.log 2>&1 &
  Windows:   pythonw pay_receive_worker.py  或 计划任务/后台服务
"""

import argparse
import json
import os
import re
import signal
import sys
import time

import requests

# 保证从项目根目录运行时可导入 scripts 及其内部相对引用（如 model.py 里的 from utils import）
_ROOT = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from scripts.config import load_config
from scripts.utils import print_with_color

# --------------- 业务 API 配置 ---------------
API_BASE = "https://test-otc-api.beingfi.com"
URL_GET_AUTO_DATA = f"{API_BASE}/Pay/PayAutoPy/getAutoDataWithApp"
URL_ORDER_QUEREN = f"{API_BASE}/Pay/PayExchange/orderQueren"
URL_PAY_SUCCESS_NOTIFY = f"{API_BASE}/Pay/PayAutoPy/paySuccessNotify"

# 状态文件：记录每个设备+APP 上次查询到的记录时间，用于“查询到上一次就停止”
STATE_FILE = os.path.join(_ROOT, "pay_receive_state.json")
# 任务工作目录（截图、日志）
WORKER_TASK_DIR = os.path.join(_ROOT, "pay_receive_worker_tasks")

# 固定参数
OP_USER_ID = 1
PAY_PASSWORD = "123456"
CURRENCY = "MMK"
RECEIVE_QUERY_MINUTES_IF_NO_STATE = 10  # 无上次记录时查询最近多少分钟

_shutdown = False


def _set_shutdown(*args):
    global _shutdown
    _shutdown = True


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print_with_color(f"保存状态失败: {e}", "red")


def get_auto_data():
    """拉取代收代付任务。成功返回 data 中 account 字典，失败返回 None。"""
    try:
        # 使用 POST 空 body；若接口要求 GET 可改为 requests.get(URL_GET_AUTO_DATA, timeout=30)
        r = requests.post(URL_GET_AUTO_DATA, json={}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "success":
            print_with_color(f"getAutoDataWithApp 非 success: {data.get('msg', data)}", "yellow")
            return None
        inner = data.get("data") or {}
        account = inner.get("account") or {}
        if not account:
            return None
        return account
    except Exception as e:
        print_with_color(f"getAutoDataWithApp 请求异常: {e}", "red")
        return None


def submit_order_queren(record):
    """
    提交单条收款确认到 orderQueren 接口。
    接口必须接收且仅使用以下 7 个参数（缺一不可）：
      returnOrderID  交易流水号的后五位
      bankcard       收款银行卡号
      amount         金额
      opUserID       业务员ID，固定为 1
      paypassword    密码，固定为 123456
      currency       币种，固定为 MMK
      date           收款时间
    record: dict 需包含 returnOrderID(或完整流水号)/bankcard/amount/date，其余由本函数固定。
    """
    # 流水号：若传入完整流水号则取后五位，否则直接使用（已是后五位）
    raw_order_id = record.get("returnOrderID", record.get("orderId", ""))
    return_order_id = str(raw_order_id)[-5:] if len(str(raw_order_id)) > 5 else str(raw_order_id)

    payload = {
        "returnOrderID": return_order_id,
        "bankcard": str(record.get("bankcard", "")),
        "amount": str(record.get("amount", "")),
        "opUserID": OP_USER_ID,
        "paypassword": PAY_PASSWORD,
        "currency": CURRENCY,
        "date": str(record.get("date", "")),
    }
    try:
        r = requests.post(URL_ORDER_QUEREN, json=payload, timeout=30)
        data = r.json() if r.ok else {}
        if data.get("status") == "success":
            return True
        print_with_color(f"orderQueren 失败: {data.get('msg', r.text)}", "red")
        return False
    except Exception as e:
        print_with_color(f"orderQueren 请求异常: {e}", "red")
        return False


def submit_pay_success_notify(orderid, success, remark=None):
    """付款结果上报。success=True 为 3，否则 9。"""
    status = 3 if success else 9
    remark = remark or ("付款成功" if success else "付款失败")
    payload = {"orderid": orderid, "status": status, "remark": remark}
    try:
        r = requests.post(URL_PAY_SUCCESS_NOTIFY, json=payload, timeout=30)
        data = r.json() if r.ok else {}
        if data.get("status") == "success":
            return True
        print_with_color(f"paySuccessNotify 失败: {data.get('msg', r.text)}", "red")
        return False
    except Exception as e:
        print_with_color(f"paySuccessNotify 请求异常: {e}", "red")
        return False


def parse_receive_records_from_response(response_text):
    """
    从一次“收款查询”指令执行后的 LLM 最终回复中解析收款记录列表。
    返回 list[dict]，每个 dict 含 returnOrderID(流水号后五位), bankcard, amount, date。
    若无法解析则返回空列表；可在此处接入真实解析（如 OCR/结构化输出）。
    """
    records = []
    if not response_text:
        return records
    # 尝试从回复中抽取类似 JSON 的列表或键值
    try:
        # 若模型返回了 JSON 数组
        for m in re.finditer(r"\[[\s\S]*?\{[^}]+\}[\s\S]*?\]", response_text):
            arr = json.loads(m.group())
            for item in arr:
                if isinstance(item, dict) and ("returnOrderID" in item or "bankcard" in item):
                    records.append({
                        "returnOrderID": str(item.get("returnOrderID", item.get("return_order_id", "")))[-5:],
                        "bankcard": str(item.get("bankcard", item.get("bank_card", ""))),
                        "amount": str(item.get("amount", item.get("money", ""))),
                        "date": str(item.get("date", item.get("time", ""))),
                    })
            if records:
                return records
    except (json.JSONDecodeError, KeyError):
        pass
    return records


def run_instruction(controller, task_desc, max_rounds, work_dir, mllm, configs):
    """
    在给定 controller（云手机 ADB）上执行一条自然语言指令，直到 FINISH 或达到 max_rounds。
    返回 (success: bool, last_response_text: str|None)。
    """
    import ast
    from scripts.and_controller import traverse_tree
    from scripts.model import parse_explore_rsp, parse_grid_rsp
    from scripts.prompts import task_template, task_template_grid
    from scripts.utils import draw_bbox_multi, draw_grid

    os.makedirs(work_dir, exist_ok=True)
    width, height = controller.get_device_size()
    if not width or not height:
        print_with_color("run_instruction: 无法获取设备尺寸", "red")
        return False, None

    round_count = 0
    last_act = "None"
    grid_on = False
    rows, cols = 0, 0
    dir_name = f"inst_{int(time.time())}"
    task_dir = os.path.join(work_dir, dir_name)
    os.makedirs(task_dir, exist_ok=True)
    log_path = os.path.join(task_dir, "log.txt")

    def area_to_xy(area, subarea):
        area -= 1
        row, col = area // cols, area % cols
        x_0, y_0 = col * (width // cols), row * (height // rows)
        if subarea == "top-left":
            x, y = x_0 + (width // cols) // 4, y_0 + (height // rows) // 4
        elif subarea == "top":
            x, y = x_0 + (width // cols) // 2, y_0 + (height // rows) // 4
        elif subarea == "top-right":
            x, y = x_0 + (width // cols) * 3 // 4, y_0 + (height // rows) // 4
        elif subarea == "left":
            x, y = x_0 + (width // cols) // 4, y_0 + (height // rows) // 2
        elif subarea == "right":
            x, y = x_0 + (width // cols) * 3 // 4, y_0 + (height // rows) // 2
        elif subarea == "bottom-left":
            x, y = x_0 + (width // cols) // 4, y_0 + (height // rows) * 3 // 4
        elif subarea == "bottom":
            x, y = x_0 + (width // cols) // 2, y_0 + (height // rows) * 3 // 4
        elif subarea == "bottom-right":
            x, y = x_0 + (width // cols) * 3 // 4, y_0 + (height // rows) * 3 // 4
        else:
            x, y = x_0 + (width // cols) // 2, y_0 + (height // rows) // 2
        return x, y

    last_response_text = None
    while round_count < max_rounds:
        round_count += 1
        screenshot_path = controller.get_screenshot(f"{dir_name}_{round_count}", task_dir)
        xml_path = controller.get_xml(f"{dir_name}_{round_count}", task_dir)
        if screenshot_path == "ERROR" or xml_path == "ERROR":
            break
        if not os.path.exists(screenshot_path):
            break
        if grid_on:
            try:
                rows, cols = draw_grid(screenshot_path, os.path.join(task_dir, f"{dir_name}_{round_count}_grid.png"))
                image = os.path.join(task_dir, f"{dir_name}_{round_count}_grid.png")
            except (ValueError, Exception):
                break
            prompt = task_template_grid
        else:
            clickable_list = []
            focusable_list = []
            traverse_tree(xml_path, clickable_list, "clickable", True)
            traverse_tree(xml_path, focusable_list, "focusable", True)
            elem_list = clickable_list.copy()
            for elem in focusable_list:
                bbox = elem.bbox
                center = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                close = False
                for e in clickable_list:
                    bbox = e.bbox
                    center_ = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                    dist = (abs(center[0] - center_[0]) ** 2 + abs(center[1] - center_[1]) ** 2) ** 0.5
                    if dist <= configs.get("MIN_DIST", 30):
                        close = True
                        break
                if not close:
                    elem_list.append(elem)
            try:
                draw_bbox_multi(
                    screenshot_path,
                    os.path.join(task_dir, f"{dir_name}_{round_count}_labeled.png"),
                    elem_list,
                    dark_mode=configs.get("DARK_MODE", False),
                )
                image = os.path.join(task_dir, f"{dir_name}_{round_count}_labeled.png")
            except (ValueError, Exception):
                break
            prompt = re.sub(r"<ui_document>", "", task_template)
        prompt = re.sub(r"<task_description>", task_desc, prompt)
        prompt = re.sub(r"<last_act>", last_act, prompt)
        status, rsp = mllm.get_model_response(prompt, [image])
        last_response_text = rsp
        if not status:
            break
        with open(log_path, "a", encoding="utf-8") as logfile:
            logfile.write(json.dumps({"step": round_count, "response": rsp}, ensure_ascii=False) + "\n")
        if grid_on:
            res = parse_grid_rsp(rsp)
        else:
            res = parse_explore_rsp(rsp)
        act_name = res[0]
        if act_name == "FINISH":
            return True, last_response_text
        if act_name == "ERROR":
            break
        last_act = res[-1]
        res = res[:-1]
        if act_name == "tap":
            _, area = res
            if area < 1 or area > len(elem_list):
                break
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            if controller.tap(x, y) == "ERROR":
                break
        elif act_name == "text":
            _, input_str = res
            if controller.text(input_str) == "ERROR":
                break
        elif act_name == "long_press":
            _, area = res
            if area < 1 or area > len(elem_list):
                break
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            if controller.long_press(x, y) == "ERROR":
                break
        elif act_name == "swipe":
            _, area, swipe_dir, dist = res
            if area < 1 or area > len(elem_list):
                break
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            if controller.swipe(x, y, swipe_dir, dist) == "ERROR":
                break
        elif act_name == "grid":
            grid_on = True
        elif act_name in ("tap_grid", "long_press_grid"):
            _, area, subarea = res
            x, y = area_to_xy(area, subarea)
            if act_name == "tap_grid":
                if controller.tap(x, y) == "ERROR":
                    break
            else:
                if controller.long_press(x, y) == "ERROR":
                    break
        elif act_name == "swipe_grid":
            _, start_area, start_subarea, end_area, end_subarea = res
            start_x, start_y = area_to_xy(start_area, start_subarea)
            end_x, end_y = area_to_xy(end_area, end_subarea)
            if controller.swipe_precise((start_x, start_y), (end_x, end_y)) == "ERROR":
                break
        if act_name != "grid":
            grid_on = False
        time.sleep(configs.get("REQUEST_INTERVAL", 10))

    return False, last_response_text


def do_receive_phase(controller, device_id, receive_apps, state, mllm, configs, max_rounds_per_instruction):
    """代收：对 receive 列表里每个 APP 执行查询指令，解析记录并提交 orderQueren，更新 state。"""
    state.setdefault(device_id, {})
    for app_name in (receive_apps or []):
        app_state = state[device_id].setdefault(app_name, {})
        last_time = app_state.get("last_time")
        if last_time:
            task_desc = f"打开{app_name},查询{last_time}后的所有收款记录"
        else:
            task_desc = f"打开{app_name},查询{RECEIVE_QUERY_MINUTES_IF_NO_STATE}分钟内的收款记录"
        work_dir = os.path.join(WORKER_TASK_DIR, device_id, "receive", app_name)
        ok, last_rsp = run_instruction(
            controller, task_desc, max_rounds_per_instruction, work_dir, mllm, configs
        )
        records = parse_receive_records_from_response(last_rsp or "")
        for rec in records:
            if submit_order_queren(rec):
                # 用当前记录时间作为下次查询起点（简化：可用 date 或 returnOrderID 对应时间）
                app_state["last_time"] = rec.get("date", last_time or "")
        if records and records[-1].get("date"):
            app_state["last_time"] = records[-1]["date"]
    save_state(state)


def do_pay_phase(controller, device_id, payment_list, mllm, configs, max_rounds_per_instruction):
    """代付：对 payment 列表每项执行付款指令，然后上报 paySuccessNotify。"""
    for item in (payment_list or []):
        app_name = item.get("payment_app", "")
        bankcard = item.get("bankcard", "")
        mum = item.get("mum", "")
        truename = item.get("truename", "")
        orderid = item.get("orderid", "")
        task_desc = f"打开{app_name}，向{bankcard}账户付款{mum}，收款人姓名为{truename}，付款密码为123456"
        work_dir = os.path.join(WORKER_TASK_DIR, device_id, "pay", orderid or str(int(time.time())))
        success, last_rsp = run_instruction(
            controller, task_desc, max_rounds_per_instruction, work_dir, mllm, configs
        )
        remark = "付款成功" if success else (last_rsp or "付款失败")[:200]
        submit_pay_success_notify(orderid, success, remark)


def main():
    global _shutdown
    parser = argparse.ArgumentParser(description="代收代付后台常驻脚本")
    parser.add_argument("--interval", type=float, default=1.0, help="每轮结束后的间隔秒数")
    parser.add_argument("--max-rounds", type=int, default=20, help="单条指令最大执行轮数")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _set_shutdown)
    signal.signal(signal.SIGTERM, _set_shutdown)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _set_shutdown)

    configs = load_config()
    # 初始化 LLM（与 task_executor 一致）
    model_type = configs.get("MODEL", "Qwen")
    if model_type == "OpenAI":
        from scripts.model import OpenAIModel
        mllm = OpenAIModel(
            base_url=configs["OPENAI_API_BASE"],
            api_key=configs["OPENAI_API_KEY"],
            model=configs["OPENAI_API_MODEL"],
            temperature=float(configs.get("TEMPERATURE", 0)),
            max_tokens=int(configs.get("MAX_TOKENS", 300)),
        )
    elif model_type == "Qwen":
        from scripts.model import QwenModel
        mllm = QwenModel(
            api_key=configs["DASHSCOPE_API_KEY"],
            model=configs.get("QWEN_MODEL", "qwen-vl-max"),
        )
    elif model_type == "DeepSeek":
        from scripts.model import DeepSeekModel
        mllm = DeepSeekModel(
            base_url=configs["DEEPSEEK_API_BASE"],
            api_key=configs["DEEPSEEK_API_KEY"],
            model=configs["DEEPSEEK_API_MODEL"],
            temperature=float(configs.get("TEMPERATURE", 0)),
            max_tokens=int(configs.get("MAX_TOKENS", 300)),
        )
    else:
        print_with_color(f"不支持的 MODEL: {model_type}", "red")
        return 1

    vmos_host = configs.get("VMOS_API_HOST", "https://api.vmoscloud.com")

    vmos_ak = configs.get("VMOS_ACCESS_KEY_ID", "")
    vmos_sk = configs.get("VMOS_SECRET_ACCESS_KEY", "")
    if not vmos_ak or not vmos_sk:
        print_with_color("请在 config.yaml 中配置 VMOS_ACCESS_KEY_ID 和 VMOS_SECRET_ACCESS_KEY", "red")
        return 1

    from scripts.vmos_cloud_controller import VMOSCloudController

    os.makedirs(WORKER_TASK_DIR, exist_ok=True)
    loop_count = 0
    while not _shutdown:
        loop_count += 1
        print_with_color(f"--- 第 {loop_count} 轮 ---", "cyan")
        account = get_auto_data()
        if not account:
            time.sleep(args.interval)
            continue
        for device_id, info in account.items():
            if _shutdown:
                break
            payment_list = info.get("payment") or []
            receive_apps = info.get("receive") or []
            if not payment_list and not receive_apps:
                continue
            controller = None
            try:
                controller = VMOSCloudController(device_id, vmos_ak, vmos_sk, vmos_host)
                if not controller.android_controller:
                    print_with_color(f"设备 {device_id} ADB 未连接，跳过", "red")
                    continue
                state = load_state()
                do_receive_phase(
                    controller, device_id, receive_apps, state,
                    mllm, configs, args.max_rounds,
                )
                do_pay_phase(
                    controller, device_id, payment_list,
                    mllm, configs, args.max_rounds,
                )
            except Exception as e:
                print_with_color(f"设备 {device_id} 处理异常: {e}", "red")
            finally:
                if controller and hasattr(controller, "cleanup"):
                    controller.cleanup()
        time.sleep(args.interval)

    print_with_color("已退出", "yellow")
    return 0


if __name__ == "__main__":
    sys.exit(main())
