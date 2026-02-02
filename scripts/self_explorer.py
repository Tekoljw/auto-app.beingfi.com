import argparse
import ast
import datetime
import json
import os
import re
import sys
import time

import prompts
from config import load_config
from and_controller import list_all_devices, AndroidController, traverse_tree
from cloud_phone_controller import CloudPhoneController, list_cloud_devices
from vmos_cloud_controller import VMOSCloudController, list_vmos_devices
from model import parse_explore_rsp, parse_reflect_rsp, OpenAIModel, QwenModel, DeepSeekModel
from utils import print_with_color, draw_bbox_multi

arg_desc = "AppAgent - Autonomous Exploration"
parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, description=arg_desc)
parser.add_argument("--app")
parser.add_argument("--root_dir", default="./")
args = vars(parser.parse_args())

configs = load_config()

if configs["MODEL"] == "OpenAI":
    mllm = OpenAIModel(base_url=configs["OPENAI_API_BASE"],
                       api_key=configs["OPENAI_API_KEY"],
                       model=configs["OPENAI_API_MODEL"],
                       temperature=configs["TEMPERATURE"],
                       max_tokens=configs["MAX_TOKENS"])
elif configs["MODEL"] == "Qwen":
    mllm = QwenModel(api_key=configs["DASHSCOPE_API_KEY"],
                     model=configs["QWEN_MODEL"])
elif configs["MODEL"] == "DeepSeek":
    mllm = DeepSeekModel(base_url=configs["DEEPSEEK_API_BASE"],
                         api_key=configs["DEEPSEEK_API_KEY"],
                         model=configs["DEEPSEEK_API_MODEL"],
                         temperature=configs["TEMPERATURE"],
                         max_tokens=configs["MAX_TOKENS"])
else:
    print_with_color(f"ERROR: Unsupported model type {configs['MODEL']}!", "red")
    sys.exit()

app = args["app"]
root_dir = args["root_dir"]

if not app:
    print_with_color("What is the name of the target app?", "blue")
    app = input()
    app = app.replace(" ", "")

work_dir = os.path.join(root_dir, "apps")
if not os.path.exists(work_dir):
    os.mkdir(work_dir)
work_dir = os.path.join(work_dir, app)
if not os.path.exists(work_dir):
    os.mkdir(work_dir)
demo_dir = os.path.join(work_dir, "demos")
if not os.path.exists(demo_dir):
    os.mkdir(demo_dir)
demo_timestamp = int(time.time())
task_name = datetime.datetime.fromtimestamp(demo_timestamp).strftime("self_explore_%Y-%m-%d_%H-%M-%S")
task_dir = os.path.join(demo_dir, task_name)
os.mkdir(task_dir)
docs_dir = os.path.join(work_dir, "auto_docs")
if not os.path.exists(docs_dir):
    os.mkdir(docs_dir)
explore_log_path = os.path.join(task_dir, f"log_explore_{task_name}.txt")
reflect_log_path = os.path.join(task_dir, f"log_reflect_{task_name}.txt")

# 根据配置选择设备连接模式
device_mode = configs.get("DEVICE_MODE", "adb").lower()

if device_mode == "vmos":
    # VMOS Cloud模式
    vmos_api_host = configs.get("VMOS_API_HOST", "api.vmoscloud.com")
    vmos_access_key_id = configs.get("VMOS_ACCESS_KEY_ID", "")
    vmos_secret_access_key = configs.get("VMOS_SECRET_ACCESS_KEY", "")
    vmos_device_id = configs.get("VMOS_DEVICE_ID", "")
    
    if not vmos_access_key_id or not vmos_secret_access_key:
        print_with_color("ERROR: VMOS Cloud模式需要配置 VMOS_ACCESS_KEY_ID 和 VMOS_SECRET_ACCESS_KEY!", "red")
        sys.exit()
    
    # 处理api_host，提取主机名用于签名
    if vmos_api_host.startswith("http://") or vmos_api_host.startswith("https://"):
        from urllib.parse import urlparse
        parsed = urlparse(vmos_api_host)
        vmos_api_host_for_sig = parsed.netloc or parsed.path
    else:
        vmos_api_host_for_sig = vmos_api_host
    
    # 获取VMOS Cloud设备列表
    device_list = list_vmos_devices(vmos_access_key_id, vmos_secret_access_key, vmos_api_host_for_sig)
    if not device_list:
        print_with_color("ERROR: 未找到可用的VMOS Cloud设备!", "red")
        sys.exit()
    
    print_with_color(f"可用的VMOS Cloud设备列表:\n{str(device_list)}", "yellow")
    
    if vmos_device_id and vmos_device_id in device_list:
        device = vmos_device_id
        print_with_color(f"使用配置的设备ID: {device}", "yellow")
    elif len(device_list) == 1:
        device = device_list[0]
        print_with_color(f"自动选择设备: {device}", "yellow")
    else:
        print_with_color("请选择要使用的VMOS Cloud设备ID (padCode):", "blue")
        device = input()
        if device not in device_list:
            print_with_color(f"ERROR: 设备ID {device} 不在可用设备列表中!", "red")
            sys.exit()
    
    controller = VMOSCloudController(device, vmos_access_key_id, vmos_secret_access_key, vmos_api_host)
elif device_mode == "cloud":
    # 通用云手机API模式
    cloud_api_base = configs.get("CLOUD_PHONE_API_BASE", "")
    cloud_api_key = configs.get("CLOUD_PHONE_API_KEY", "")
    cloud_device_id = configs.get("CLOUD_PHONE_DEVICE_ID", "")
    
    if not cloud_api_base:
        print_with_color("ERROR: 云手机模式需要配置 CLOUD_PHONE_API_BASE!", "red")
        sys.exit()
    
    # 获取云手机设备列表
    device_list = list_cloud_devices(cloud_api_base, cloud_api_key)
    if not device_list:
        print_with_color("ERROR: 未找到可用的云手机设备!", "red")
        sys.exit()
    
    print_with_color(f"可用的云手机设备列表:\n{str(device_list)}", "yellow")
    
    if cloud_device_id and cloud_device_id in device_list:
        device = cloud_device_id
        print_with_color(f"使用配置的设备ID: {device}", "yellow")
    elif len(device_list) == 1:
        device = device_list[0]
        print_with_color(f"自动选择设备: {device}", "yellow")
    else:
        print_with_color("请选择要使用的云手机设备ID:", "blue")
        device = input()
        if device not in device_list:
            print_with_color(f"ERROR: 设备ID {device} 不在可用设备列表中!", "red")
            sys.exit()
    
    controller = CloudPhoneController(device, cloud_api_base, cloud_api_key)
else:
    # 本地ADB模式（默认）
    device_list = list_all_devices()
    if not device_list:
        print_with_color("ERROR: No device found!", "red")
        sys.exit()
    print_with_color(f"List of devices attached:\n{str(device_list)}", "yellow")
    if len(device_list) == 1:
        device = device_list[0]
        print_with_color(f"Device selected: {device}", "yellow")
    else:
        print_with_color("Please choose the Android device to start demo by entering its ID:", "blue")
        device = input()
    controller = AndroidController(device)

width, height = controller.get_device_size()
if not width and not height:
    print_with_color("ERROR: Invalid device size!", "red")
    sys.exit()
print_with_color(f"Screen resolution of {device}: {width}x{height}", "yellow")

print_with_color("Please enter the description of the task you want me to complete in a few sentences:", "blue")
task_desc = input()

round_count = 0
doc_count = 0
useless_list = set()
last_act = "None"
task_complete = False
while round_count < configs["MAX_ROUNDS"]:
    round_count += 1
    print_with_color(f"Round {round_count}", "yellow")
    screenshot_before = controller.get_screenshot(f"{round_count}_before", task_dir)
    xml_path = controller.get_xml(f"{round_count}", task_dir)
    if screenshot_before == "ERROR" or xml_path == "ERROR":
        break
    clickable_list = []
    focusable_list = []
    traverse_tree(xml_path, clickable_list, "clickable", True)
    traverse_tree(xml_path, focusable_list, "focusable", True)
    elem_list = []
    for elem in clickable_list:
        if elem.uid in useless_list:
            continue
        elem_list.append(elem)
    for elem in focusable_list:
        if elem.uid in useless_list:
            continue
        bbox = elem.bbox
        center = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
        close = False
        for e in clickable_list:
            bbox = e.bbox
            center_ = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
            dist = (abs(center[0] - center_[0]) ** 2 + abs(center[1] - center_[1]) ** 2) ** 0.5
            if dist <= configs["MIN_DIST"]:
                close = True
                break
        if not close:
            elem_list.append(elem)
    draw_bbox_multi(screenshot_before, os.path.join(task_dir, f"{round_count}_before_labeled.png"), elem_list,
                    dark_mode=configs["DARK_MODE"])

    prompt = re.sub(r"<task_description>", task_desc, prompts.self_explore_task_template)
    prompt = re.sub(r"<last_act>", last_act, prompt)
    base64_img_before = os.path.join(task_dir, f"{round_count}_before_labeled.png")
    print_with_color("Thinking about what to do in the next step...", "yellow")
    status, rsp = mllm.get_model_response(prompt, [base64_img_before])

    if status:
        with open(explore_log_path, "a") as logfile:
            log_item = {"step": round_count, "prompt": prompt, "image": f"{round_count}_before_labeled.png",
                        "response": rsp}
            logfile.write(json.dumps(log_item) + "\n")
        res = parse_explore_rsp(rsp)
        act_name = res[0]
        last_act = res[-1]
        res = res[:-1]
        if act_name == "FINISH":
            task_complete = True
            break
        if act_name == "tap":
            _, area = res
            # 检查area是否在有效范围内
            if area < 1 or area > len(elem_list):
                print_with_color(f"ERROR: 元素编号 {area} 超出范围 (1-{len(elem_list)})，当前屏幕有 {len(elem_list)} 个可交互元素", "red")
                print_with_color("提示: 可能是UI层级解析失败或元素数量发生变化，尝试继续下一轮", "yellow")
                continue
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            ret = controller.tap(x, y)
            if ret == "ERROR":
                print_with_color("ERROR: tap execution failed", "red")
                break
        elif act_name == "text":
            _, input_str = res
            ret = controller.text(input_str)
            if ret == "ERROR":
                print_with_color("ERROR: text execution failed", "red")
                break
        elif act_name == "long_press":
            _, area = res
            # 检查area是否在有效范围内
            if area < 1 or area > len(elem_list):
                print_with_color(f"ERROR: 元素编号 {area} 超出范围 (1-{len(elem_list)})，当前屏幕有 {len(elem_list)} 个可交互元素", "red")
                print_with_color("提示: 可能是UI层级解析失败或元素数量发生变化，尝试继续下一轮", "yellow")
                continue
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            ret = controller.long_press(x, y)
            if ret == "ERROR":
                print_with_color("ERROR: long press execution failed", "red")
                break
        elif act_name == "swipe":
            _, area, swipe_dir, dist = res
            # 检查area是否在有效范围内
            if area < 1 or area > len(elem_list):
                print_with_color(f"ERROR: 元素编号 {area} 超出范围 (1-{len(elem_list)})，当前屏幕有 {len(elem_list)} 个可交互元素", "red")
                print_with_color("提示: 可能是UI层级解析失败或元素数量发生变化，尝试继续下一轮", "yellow")
                continue
            tl, br = elem_list[area - 1].bbox
            x, y = (tl[0] + br[0]) // 2, (tl[1] + br[1]) // 2
            ret = controller.swipe(x, y, swipe_dir, dist)
            if ret == "ERROR":
                print_with_color("ERROR: swipe execution failed", "red")
                break
        else:
            break
        time.sleep(configs["REQUEST_INTERVAL"])
    else:
        print_with_color(rsp, "red")
        break

    screenshot_after = controller.get_screenshot(f"{round_count}_after", task_dir)
    if screenshot_after == "ERROR":
        break
    draw_bbox_multi(screenshot_after, os.path.join(task_dir, f"{round_count}_after_labeled.png"), elem_list,
                    dark_mode=configs["DARK_MODE"])
    base64_img_after = os.path.join(task_dir, f"{round_count}_after_labeled.png")

    if act_name == "tap":
        prompt = re.sub(r"<action>", "tapping", prompts.self_explore_reflect_template)
    elif act_name == "text":
        continue
    elif act_name == "long_press":
        prompt = re.sub(r"<action>", "long pressing", prompts.self_explore_reflect_template)
    elif act_name == "swipe":
        swipe_dir = res[2]
        if swipe_dir == "up" or swipe_dir == "down":
            act_name = "v_swipe"
        elif swipe_dir == "left" or swipe_dir == "right":
            act_name = "h_swipe"
        prompt = re.sub(r"<action>", "swiping", prompts.self_explore_reflect_template)
    else:
        print_with_color("ERROR: Undefined act!", "red")
        break
    prompt = re.sub(r"<ui_element>", str(area), prompt)
    prompt = re.sub(r"<task_desc>", task_desc, prompt)
    prompt = re.sub(r"<last_act>", last_act, prompt)

    print_with_color("Reflecting on my previous action...", "yellow")
    status, rsp = mllm.get_model_response(prompt, [base64_img_before, base64_img_after])
    if status:
        # 检查area是否在有效范围内
        area_int = int(area)
        if area_int < 1 or area_int > len(elem_list):
            print_with_color(f"ERROR: 元素编号 {area_int} 超出范围 (1-{len(elem_list)})，跳过文档生成", "red")
            continue
        resource_id = elem_list[area_int - 1].uid
        with open(reflect_log_path, "a") as logfile:
            log_item = {"step": round_count, "prompt": prompt, "image_before": f"{round_count}_before_labeled.png",
                        "image_after": f"{round_count}_after.png", "response": rsp}
            logfile.write(json.dumps(log_item) + "\n")
        res = parse_reflect_rsp(rsp)
        decision = res[0]
        if decision == "ERROR":
            break
        if decision == "INEFFECTIVE":
            useless_list.add(resource_id)
            last_act = "None"
        elif decision == "BACK" or decision == "CONTINUE" or decision == "SUCCESS":
            if decision == "BACK" or decision == "CONTINUE":
                useless_list.add(resource_id)
                last_act = "None"
                if decision == "BACK":
                    ret = controller.back()
                    if ret == "ERROR":
                        print_with_color("ERROR: back execution failed", "red")
                        break
            doc = res[-1]
            doc_name = resource_id + ".txt"
            doc_path = os.path.join(docs_dir, doc_name)
            if os.path.exists(doc_path):
                doc_content = ast.literal_eval(open(doc_path).read())
                if doc_content[act_name]:
                    print_with_color(f"Documentation for the element {resource_id} already exists.", "yellow")
                    continue
            else:
                doc_content = {
                    "tap": "",
                    "text": "",
                    "v_swipe": "",
                    "h_swipe": "",
                    "long_press": ""
                }
            doc_content[act_name] = doc
            with open(doc_path, "w") as outfile:
                outfile.write(str(doc_content))
            doc_count += 1
            print_with_color(f"Documentation generated and saved to {doc_path}", "yellow")
        else:
            print_with_color(f"ERROR: Undefined decision! {decision}", "red")
            break
    else:
        print_with_color(rsp["error"]["message"], "red")
        break
    time.sleep(configs["REQUEST_INTERVAL"])

if task_complete:
    print_with_color(f"Autonomous exploration completed successfully. {doc_count} docs generated.", "yellow")
elif round_count == configs["MAX_ROUNDS"]:
    print_with_color(f"Autonomous exploration finished due to reaching max rounds. {doc_count} docs generated.",
                     "yellow")
else:
    print_with_color(f"Autonomous exploration finished unexpectedly. {doc_count} docs generated.", "red")
