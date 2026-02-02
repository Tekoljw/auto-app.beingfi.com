"""
云手机API控制器
用于通过HTTP API远程操作云手机，替代本地ADB连接
"""
import os
import requests
import base64
from typing import Tuple, Optional
from config import load_config
from utils import print_with_color

configs = load_config()


class CloudPhoneController:
    """云手机API控制器，实现与AndroidController相同的接口"""
    
    def __init__(self, device_id: str, api_base_url: str, api_key: str = None):
        """
        初始化云手机控制器
        
        Args:
            device_id: 云手机设备ID
            api_base_url: 云手机API基础URL (例如: https://api.cloudphone.com/v1)
            api_key: API密钥（如果需要认证）
        """
        self.device_id = device_id
        self.api_base_url = api_base_url.rstrip('/')
        self.api_key = api_key
        self.headers = {}
        if api_key:
            self.headers['Authorization'] = f'Bearer {api_key}'
            self.headers['X-API-Key'] = api_key
        
        # 获取设备信息
        self.width, self.height = self.get_device_size()
        if not self.width or not self.height:
            print_with_color("WARNING: 无法获取设备尺寸，使用默认值 1080x1920", "yellow")
            self.width, self.height = 1080, 1920
    
    def _make_request(self, method: str, endpoint: str, params: dict = None, 
                     json_data: dict = None, files: dict = None) -> dict:
        """
        发送HTTP请求到云手机API
        
        Args:
            method: HTTP方法 (GET, POST, PUT等)
            endpoint: API端点路径
            params: URL参数
            json_data: JSON请求体
            files: 文件上传
            
        Returns:
            API响应结果
        """
        url = f"{self.api_base_url}/{endpoint.lstrip('/')}"
        
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=self.headers, params=params, timeout=30)
            elif method.upper() == 'POST':
                if files:
                    response = requests.post(url, headers=self.headers, data=params, 
                                           files=files, timeout=30)
                else:
                    response = requests.post(url, headers=self.headers, params=params, 
                                            json=json_data, timeout=30)
            elif method.upper() == 'PUT':
                response = requests.put(url, headers=self.headers, params=params, 
                                      json=json_data, timeout=30)
            else:
                return {"error": f"Unsupported HTTP method: {method}"}
            
            response.raise_for_status()
            return response.json() if response.content else {"status": "success"}
            
        except requests.exceptions.RequestException as e:
            print_with_color(f"API请求失败: {endpoint}, 错误: {str(e)}", "red")
            return {"error": str(e)}
    
    def get_device_size(self) -> Tuple[int, int]:
        """
        获取设备屏幕尺寸
        
        Returns:
            (width, height) 元组
        """
        # 方法1: 通过API获取设备信息
        result = self._make_request('GET', f'/devices/{self.device_id}/info')
        if 'error' not in result:
            if 'width' in result and 'height' in result:
                return int(result['width']), int(result['height'])
            elif 'screen' in result:
                screen = result['screen']
                return int(screen.get('width', 1080)), int(screen.get('height', 1920))
        
        # 方法2: 通过截图获取尺寸（备用方案）
        screenshot_result = self._make_request('POST', f'/devices/{self.device_id}/screenshot')
        if 'error' not in screenshot_result and 'width' in screenshot_result:
            return int(screenshot_result['width']), int(screenshot_result['height'])
        
        return 0, 0
    
    def get_screenshot(self, prefix: str, save_dir: str) -> str:
        """
        获取屏幕截图
        
        Args:
            prefix: 文件名前缀
            save_dir: 保存目录
            
        Returns:
            截图文件路径，失败返回 "ERROR"
        """
        # 调用云手机API获取截图
        result = self._make_request('POST', f'/devices/{self.device_id}/screenshot')
        
        if 'error' in result:
            print_with_color(f"获取截图失败: {result['error']}", "red")
            return "ERROR"
        
        # 处理截图数据（可能是base64编码或URL）
        image_data = None
        if 'image' in result:
            # Base64编码的图片
            image_data = base64.b64decode(result['image'])
        elif 'image_url' in result:
            # 图片URL，需要下载
            try:
                img_response = requests.get(result['image_url'], timeout=30)
                img_response.raise_for_status()
                image_data = img_response.content
            except Exception as e:
                print_with_color(f"下载截图失败: {str(e)}", "red")
                return "ERROR"
        elif 'image_base64' in result:
            # 另一种可能的字段名
            image_data = base64.b64decode(result['image_base64'])
        
        if not image_data:
            print_with_color("API返回的截图数据格式不正确", "red")
            return "ERROR"
        
        # 保存截图
        file_path = os.path.join(save_dir, f"{prefix}.png")
        try:
            os.makedirs(save_dir, exist_ok=True)
            with open(file_path, 'wb') as f:
                f.write(image_data)
            
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                return file_path
            else:
                print_with_color(f"截图文件保存失败或为空: {file_path}", "red")
                return "ERROR"
        except Exception as e:
            print_with_color(f"保存截图失败: {str(e)}", "red")
            return "ERROR"
    
    def get_xml(self, prefix: str, save_dir: str) -> str:
        """
        获取UI层级XML
        
        Args:
            prefix: 文件名前缀
            save_dir: 保存目录
            
        Returns:
            XML文件路径，失败返回 "ERROR"
        """
        # 调用云手机API获取UI层级
        result = self._make_request('POST', f'/devices/{self.device_id}/ui_dump')
        
        if 'error' in result:
            print_with_color(f"获取UI层级失败: {result['error']}", "red")
            return "ERROR"
        
        # 处理XML数据
        xml_content = None
        if 'xml' in result:
            xml_content = result['xml']
        elif 'ui_dump' in result:
            xml_content = result['ui_dump']
        elif 'xml_content' in result:
            xml_content = result['xml_content']
        
        if not xml_content:
            print_with_color("API返回的XML数据格式不正确", "red")
            return "ERROR"
        
        # 保存XML文件
        file_path = os.path.join(save_dir, f"{prefix}.xml")
        try:
            os.makedirs(save_dir, exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(xml_content)
            
            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                return file_path
            else:
                print_with_color(f"XML文件保存失败或为空: {file_path}", "red")
                return "ERROR"
        except Exception as e:
            print_with_color(f"保存XML失败: {str(e)}", "red")
            return "ERROR"
    
    def back(self) -> str:
        """按返回键"""
        result = self._make_request('POST', f'/devices/{self.device_id}/input/keyevent', 
                                   json_data={'keycode': 'BACK'})
        return "ERROR" if 'error' in result else result.get('status', 'success')
    
    def tap(self, x: int, y: int) -> str:
        """
        点击坐标
        
        Args:
            x: X坐标
            y: Y坐标
        """
        result = self._make_request('POST', f'/devices/{self.device_id}/input/tap', 
                                   json_data={'x': x, 'y': y})
        if 'error' in result:
            print_with_color(f"点击操作失败: {result['error']}", "red")
            return "ERROR"
        return result.get('status', 'success')
    
    def text(self, input_str: str) -> str:
        """
        输入文本
        
        Args:
            input_str: 要输入的文本
        """
        result = self._make_request('POST', f'/devices/{self.device_id}/input/text', 
                                   json_data={'text': input_str})
        if 'error' in result:
            print_with_color(f"文本输入失败: {result['error']}", "red")
            return "ERROR"
        return result.get('status', 'success')
    
    def long_press(self, x: int, y: int, duration: int = 1000) -> str:
        """
        长按坐标
        
        Args:
            x: X坐标
            y: Y坐标
            duration: 持续时间（毫秒）
        """
        result = self._make_request('POST', f'/devices/{self.device_id}/input/long_press', 
                                   json_data={'x': x, 'y': y, 'duration': duration})
        if 'error' in result:
            print_with_color(f"长按操作失败: {result['error']}", "red")
            return "ERROR"
        return result.get('status', 'success')
    
    def swipe(self, x: int, y: int, direction: str, dist: str = "medium", quick: bool = False) -> str:
        """
        滑动操作
        
        Args:
            x: 起始X坐标
            y: 起始Y坐标
            direction: 滑动方向 (up/down/left/right)
            dist: 滑动距离 (short/medium/long)
            quick: 是否快速滑动
        """
        unit_dist = int(self.width / 10)
        if dist == "long":
            unit_dist *= 3
        elif dist == "medium":
            unit_dist *= 2
        
        if direction == "up":
            offset = (0, -2 * unit_dist)
        elif direction == "down":
            offset = (0, 2 * unit_dist)
        elif direction == "left":
            offset = (-1 * unit_dist, 0)
        elif direction == "right":
            offset = (unit_dist, 0)
        else:
            return "ERROR"
        
        end_x = x + offset[0]
        end_y = y + offset[1]
        duration = 100 if quick else 400
        
        result = self._make_request('POST', f'/devices/{self.device_id}/input/swipe', 
                                   json_data={
                                       'start_x': x, 'start_y': y,
                                       'end_x': end_x, 'end_y': end_y,
                                       'duration': duration
                                   })
        if 'error' in result:
            print_with_color(f"滑动操作失败: {result['error']}", "red")
            return "ERROR"
        return result.get('status', 'success')
    
    def swipe_precise(self, start: Tuple[int, int], end: Tuple[int, int], duration: int = 400) -> str:
        """
        精确滑动
        
        Args:
            start: 起始坐标 (x, y)
            end: 结束坐标 (x, y)
            duration: 滑动持续时间（毫秒）
        """
        start_x, start_y = start
        end_x, end_y = end
        
        result = self._make_request('POST', f'/devices/{self.device_id}/input/swipe', 
                                   json_data={
                                       'start_x': start_x, 'start_y': start_y,
                                       'end_x': end_x, 'end_y': end_y,
                                       'duration': duration
                                   })
        if 'error' in result:
            print_with_color(f"精确滑动失败: {result['error']}", "red")
            return "ERROR"
        return result.get('status', 'success')


def list_cloud_devices(api_base_url: str, api_key: str = None) -> list:
    """
    列出所有可用的云手机设备
    
    Args:
        api_base_url: 云手机API基础URL
        api_key: API密钥
        
    Returns:
        设备ID列表
    """
    headers = {}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
        headers['X-API-Key'] = api_key
    
    try:
        url = f"{api_base_url.rstrip('/')}/devices"
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        result = response.json()
        
        device_list = []
        if 'devices' in result:
            for device in result['devices']:
                device_list.append(device.get('id', device.get('device_id', '')))
        elif isinstance(result, list):
            for device in result:
                device_list.append(device.get('id', device.get('device_id', '')))
        
        return device_list
    except Exception as e:
        print_with_color(f"获取云手机设备列表失败: {str(e)}", "red")
        return []




