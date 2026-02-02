import os
import subprocess
import xml.etree.ElementTree as ET

from config import load_config
from utils import print_with_color


configs = load_config()


class AndroidElement:
    def __init__(self, uid, bbox, attrib):
        self.uid = uid
        self.bbox = bbox
        self.attrib = attrib


def execute_adb(adb_command):
    # print(adb_command)
    # 在 Windows 上，确保使用正确的编码
    result = subprocess.run(adb_command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                          text=True, encoding='utf-8', errors='replace')
    if result.returncode == 0:
        return result.stdout.strip()
    print_with_color(f"Command execution failed: {adb_command}", "red")
    if result.stderr:
        print_with_color(f"Error output: {result.stderr}", "red")
    if result.stdout:
        print_with_color(f"Standard output: {result.stdout}", "red")
    return "ERROR"


def list_all_devices():
    adb_command = "adb devices"
    device_list = []
    result = execute_adb(adb_command)
    if result != "ERROR":
        devices = result.split("\n")[1:]
        for d in devices:
            device_list.append(d.split()[0])

    return device_list


def get_id_from_element(elem):
    bounds = elem.attrib["bounds"][1:-1].split("][")
    x1, y1 = map(int, bounds[0].split(","))
    x2, y2 = map(int, bounds[1].split(","))
    elem_w, elem_h = x2 - x1, y2 - y1
    if "resource-id" in elem.attrib and elem.attrib["resource-id"]:
        elem_id = elem.attrib["resource-id"].replace(":", ".").replace("/", "_")
    else:
        elem_id = f"{elem.attrib['class']}_{elem_w}_{elem_h}"
    if "content-desc" in elem.attrib and elem.attrib["content-desc"] and len(elem.attrib["content-desc"]) < 20:
        content_desc = elem.attrib['content-desc'].replace("/", "_").replace(" ", "").replace(":", "_")
        elem_id += f"_{content_desc}"
    return elem_id


def traverse_tree(xml_path, elem_list, attrib, add_index=False):
    path = []
    for event, elem in ET.iterparse(xml_path, ['start', 'end']):
        if event == 'start':
            path.append(elem)
            if attrib in elem.attrib and elem.attrib[attrib] == "true":
                parent_prefix = ""
                if len(path) > 1:
                    parent_prefix = get_id_from_element(path[-2])
                bounds = elem.attrib["bounds"][1:-1].split("][")
                x1, y1 = map(int, bounds[0].split(","))
                x2, y2 = map(int, bounds[1].split(","))
                center = (x1 + x2) // 2, (y1 + y2) // 2
                elem_id = get_id_from_element(elem)
                if parent_prefix:
                    elem_id = parent_prefix + "_" + elem_id
                if add_index:
                    elem_id += f"_{elem.attrib['index']}"
                close = False
                for e in elem_list:
                    bbox = e.bbox
                    center_ = (bbox[0][0] + bbox[1][0]) // 2, (bbox[0][1] + bbox[1][1]) // 2
                    dist = (abs(center[0] - center_[0]) ** 2 + abs(center[1] - center_[1]) ** 2) ** 0.5
                    if dist <= configs["MIN_DIST"]:
                        close = True
                        break
                if not close:
                    elem_list.append(AndroidElement(elem_id, ((x1, y1), (x2, y2)), attrib))

        if event == 'end':
            path.pop()


class AndroidController:
    def __init__(self, device):
        self.device = device
        self.screenshot_dir = configs["ANDROID_SCREENSHOT_DIR"]
        self.xml_dir = configs["ANDROID_XML_DIR"]
        self.width, self.height = self.get_device_size()
        self.backslash = "\\"

    def get_device_size(self):
        adb_command = f"adb -s {self.device} shell wm size"
        result = execute_adb(adb_command)
        if result != "ERROR":
            try:
                # adb shell wm size 可能返回多种格式：
                # 1. "Physical size: 1080x1920"
                # 2. "1080x1920"
                # 3. "Physical size: 1080x1920\nOverride size: 1080x1920"
                # 4. 多行格式
                
                # 先按行分割
                lines = result.strip().split('\n')
                
                # 查找包含 "x" 的行（应该是尺寸信息）
                size_str = None
                for line in lines:
                    line = line.strip()
                    if 'x' in line and not line.startswith('Override'):
                        # 提取尺寸部分
                        if ':' in line:
                            # 格式: "Physical size: 1080x1920"
                            parts = line.split(':')
                            if len(parts) >= 2:
                                size_str = parts[1].strip()
                                break
                        elif line.count('x') == 1:
                            # 格式: "1080x1920"
                            size_str = line
                            break
                
                if size_str:
                    # 解析 widthxheight
                    width, height = size_str.split('x')
                    return int(width.strip()), int(height.strip())
                else:
                    # 如果无法解析，尝试直接解析整个结果
                    # 移除所有非数字和x的字符，然后查找第一个 widthxheight 模式
                    import re
                    match = re.search(r'(\d+)x(\d+)', result)
                    if match:
                        return int(match.group(1)), int(match.group(2))
                    
                    print_with_color(f"警告: 无法解析设备尺寸，返回默认值 1080x1920。原始输出: {result}", "yellow")
                    return 1080, 1920
            except Exception as e:
                print_with_color(f"解析设备尺寸时出错: {str(e)}，返回默认值 1080x1920。原始输出: {result}", "yellow")
                return 1080, 1920
        return 1080, 1920

    def get_screenshot(self, prefix, save_dir):
        remote_path = os.path.join(self.screenshot_dir, prefix + '.png').replace(self.backslash, '/')
        local_path = os.path.join(save_dir, prefix + '.png')
        cap_command = f"adb -s {self.device} shell screencap -p {remote_path}"
        # 使用引号包裹路径以处理包含中文的情况
        pull_command = f'adb -s {self.device} pull "{remote_path}" "{local_path}"'
        result = execute_adb(cap_command)
        if result != "ERROR":
            result = execute_adb(pull_command)
            if result != "ERROR":
                file_path = os.path.join(save_dir, prefix + ".png")
                # 验证文件是否真的存在
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    return file_path
                else:
                    print_with_color(f"ERROR: Screenshot file was not properly pulled or is empty: {file_path}", "red")
                    return "ERROR"
            return result
        return result

    def get_xml(self, prefix, save_dir):
        remote_path = os.path.join(self.xml_dir, prefix + '.xml').replace(self.backslash, '/')
        local_path = os.path.join(save_dir, prefix + '.xml')
        dump_command = f"adb -s {self.device} shell uiautomator dump {remote_path}"
        # 使用引号包裹路径以处理包含中文的情况
        pull_command = f'adb -s {self.device} pull "{remote_path}" "{local_path}"'
        result = execute_adb(dump_command)
        if result != "ERROR":
            result = execute_adb(pull_command)
            if result != "ERROR":
                file_path = os.path.join(save_dir, prefix + ".xml")
                # 验证文件是否真的存在
                if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                    return file_path
                else:
                    print_with_color(f"ERROR: XML file was not properly pulled or is empty: {file_path}", "red")
                    return "ERROR"
            return result
        return result

    def back(self):
        adb_command = f"adb -s {self.device} shell input keyevent KEYCODE_BACK"
        ret = execute_adb(adb_command)
        return ret

    def tap(self, x, y):
        # 尝试使用标准的 input tap 命令
        adb_command = f"adb -s {self.device} shell input tap {x} {y}"
        ret = execute_adb(adb_command)
        if ret != "ERROR":
            return ret
        
        # 如果失败，提供详细的错误信息和解决建议
        print_with_color(f"ERROR: tap 操作失败。这是 Android 权限问题 (INJECT_EVENTS permission required)。", "red")
        print_with_color(f"解决方案：", "yellow")
        print_with_color(f"1. 在设备的 设置 -> 开发者选项 中启用 'USB调试（安全设置）'", "yellow")
        print_with_color(f"2. 某些设备需要在开发者选项中启用 '禁用权限监控' 或 '允许模拟输入'", "yellow")
        print_with_color(f"3. 如果设备是 Android 11+，可能需要启用 '无线调试' 并使用配对码连接", "yellow")
        print_with_color(f"4. 某些厂商设备（如小米、华为）需要在开发者选项中额外启用相关权限", "yellow")
        print_with_color(f"5. 如果以上方法都不行，可能需要 root 权限或使用其他自动化工具", "yellow")
        return "ERROR"

    def text(self, input_str):
        # 检查是否包含中文字符
        has_chinese = any('\u4e00' <= char <= '\u9fff' for char in input_str)
        
        if has_chinese:
            # 对于包含中文的文本，使用 IME 方法
            # 方法：通过 am broadcast 发送文本到支持 ADB 的输入法
            # 或者使用剪贴板 + 粘贴的方法
            
            # 尝试方法1: 使用 service call 设置剪贴板（需要 root 或特殊权限）
            # 将文本编码为 base64 以避免特殊字符问题
            import base64
            try:
                # 将文本编码为 base64
                text_bytes = input_str.encode('utf-8')
                text_b64 = base64.b64encode(text_bytes).decode('ascii')
                
                # 使用 service call 设置剪贴板（Android 11+）
                # 注意：这需要设备支持或 root 权限
                clipboard_cmd = f'adb -s {self.device} shell "service call clipboard 1 i32 1 i32 0 s16 \\"com.android.shell\\" s16 \\"{text_b64}\\""'
                ret = execute_adb(clipboard_cmd)
                
                # 如果成功，尝试粘贴
                if ret != "ERROR":
                    paste_cmd = f"adb -s {self.device} shell input keyevent KEYCODE_PASTE"
                    ret = execute_adb(paste_cmd)
                    if ret != "ERROR":
                        return ret
                
                # 方法2: 使用 am broadcast 配合支持 ADB 输入的输入法
                # 这需要设备上安装了支持 ADB 输入的输入法（如某些第三方输入法）
                print_with_color(f"警告: 标准方法无法输入中文，尝试使用备用方法...", "yellow")
                
                # 方法3: 使用 IME 命令（如果设备支持）
                # 先获取当前输入法
                ime_cmd = f"adb -s {self.device} shell ime list -s"
                ime_result = execute_adb(ime_cmd)
                
                # 尝试使用 IME 输入
                # 注意：这需要特定的输入法支持
                print_with_color(f"ERROR: 无法输入包含中文的文本: {input_str}", "red")
                print_with_color(f"提示: adb shell input text 不支持中文字符。", "yellow")
                print_with_color(f"解决方案：", "yellow")
                print_with_color(f"1. 安装支持 ADB 输入的输入法（如某些第三方输入法）", "yellow")
                print_with_color(f"2. 使用 root 权限的设备", "yellow")
                print_with_color(f"3. 或者手动在设备上输入文本", "yellow")
                return "ERROR"
                
            except Exception as e:
                print_with_color(f"ERROR: 输入中文文本时发生异常: {str(e)}", "red")
                return "ERROR"
        else:
            # 对于纯 ASCII 文本，使用标准方法
            # 转义特殊字符
            input_str = input_str.replace(" ", "%s")
            input_str = input_str.replace("'", "")
            input_str = input_str.replace("&", "\\&")
            input_str = input_str.replace("|", "\\|")
            input_str = input_str.replace(";", "\\;")
            input_str = input_str.replace("<", "\\<")
            input_str = input_str.replace(">", "\\>")
            input_str = input_str.replace("(", "\\(")
            input_str = input_str.replace(")", "\\)")
            input_str = input_str.replace("$", "\\$")
            input_str = input_str.replace("`", "\\`")
            input_str = input_str.replace("\\", "\\\\")
            
        adb_command = f"adb -s {self.device} shell input text {input_str}"
        ret = execute_adb(adb_command)
        return ret

    def long_press(self, x, y, duration=1000):
        adb_command = f"adb -s {self.device} shell input swipe {x} {y} {x} {y} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def swipe(self, x, y, direction, dist="medium", quick=False):
        unit_dist = int(self.width / 10)
        if dist == "long":
            unit_dist *= 3
        elif dist == "medium":
            unit_dist *= 2
        if direction == "up":
            offset = 0, -2 * unit_dist
        elif direction == "down":
            offset = 0, 2 * unit_dist
        elif direction == "left":
            offset = -1 * unit_dist, 0
        elif direction == "right":
            offset = unit_dist, 0
        else:
            return "ERROR"
        duration = 100 if quick else 400
        adb_command = f"adb -s {self.device} shell input swipe {x} {y} {x+offset[0]} {y+offset[1]} {duration}"
        ret = execute_adb(adb_command)
        return ret

    def swipe_precise(self, start, end, duration=400):
        start_x, start_y = start
        end_x, end_y = end
        adb_command = f"adb -s {self.device} shell input swipe {start_x} {start_x} {end_x} {end_y} {duration}"
        ret = execute_adb(adb_command)
        return ret
