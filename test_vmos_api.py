"""
VMOS Cloud API 测试脚本
用于测试VMOS Cloud API连接和基本功能
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from scripts.vmos_cloud_controller import VMOSCloudController, list_vmos_devices
from scripts.config import load_config
from scripts.utils import print_with_color

def test_vmos_connection():
    """测试VMOS Cloud API连接"""
    configs = load_config()
    
    api_host = configs.get("VMOS_API_HOST", "api.vmoscloud.com")
    # 如果配置中包含协议，需要提取主机名用于签名
    if api_host.startswith("http://") or api_host.startswith("https://"):
        from urllib.parse import urlparse
        parsed = urlparse(api_host)
        api_host_for_sig = parsed.netloc or parsed.path
    else:
        api_host_for_sig = api_host
    access_key_id = configs.get("VMOS_ACCESS_KEY_ID", "")
    secret_access_key = configs.get("VMOS_SECRET_ACCESS_KEY", "")
    vmos_device_id = configs.get("VMOS_DEVICE_ID", "")
    
    if not access_key_id or not secret_access_key:
        print_with_color("ERROR: 请在config.yaml中配置VMOS_ACCESS_KEY_ID和VMOS_SECRET_ACCESS_KEY", "red")
        return False
    
    print_with_color("=" * 50, "cyan")
    print_with_color("VMOS Cloud API 连接测试", "cyan")
    print_with_color("=" * 50, "cyan")
    print_with_color(f"API Host: {api_host}", "yellow")
    print_with_color(f"Access Key ID: {access_key_id[:10]}...", "yellow")
    if vmos_device_id:
        print_with_color(f"设备ID (从配置文件): {vmos_device_id}", "yellow")
    print_with_color("", "white")
    
    # 优先使用配置文件中的设备ID，但先验证设备是否存在
    device_id = None
    if vmos_device_id:
        print_with_color(f"配置文件中的设备ID: {vmos_device_id}", "yellow")
        print_with_color("提示: 如果设备不存在，将尝试获取设备列表", "yellow")
        device_id = vmos_device_id
    else:
        # 如果没有配置设备ID，尝试获取设备列表
        print_with_color("测试1: 获取设备列表...", "blue")
        try:
            device_list = list_vmos_devices(access_key_id, secret_access_key, api_host_for_sig)
            if device_list:
                print_with_color(f"✓ 成功获取 {len(device_list)} 个设备", "green")
                print_with_color(f"设备列表: {device_list}", "yellow")
                
                # 使用第一个设备
                if device_list:
                    device_id = device_list[0]
                    print_with_color(f"使用设备列表中的第一个设备: {device_id}", "yellow")
                else:
                    print_with_color("✗ 没有可用设备", "red")
                    print_with_color("提示: 请在config.yaml中配置VMOS_DEVICE_ID", "yellow")
                    return False
            else:
                print_with_color("✗ 无法获取设备列表", "red")
                print_with_color("提示: 请在config.yaml中配置VMOS_DEVICE_ID", "yellow")
                return False
        except Exception as e:
            print_with_color(f"✗ 获取设备列表失败: {str(e)}", "red")
            print_with_color("提示: 请在config.yaml中配置VMOS_DEVICE_ID", "yellow")
            return False
    
    if not device_id:
        print_with_color("✗ 无法获取设备ID", "red")
        return False
    
    # 测试2: 连接设备
    print_with_color("", "white")
    print_with_color(f"测试2: 连接设备 {device_id}...", "blue")
    try:
        controller = VMOSCloudController(device_id, access_key_id, secret_access_key, api_host)
        
        # 如果设备不存在，尝试获取设备列表
        print_with_color("测试2.1: 验证设备是否存在...", "blue")
        test_result = controller._make_request('POST', '/vcpcloud/api/padApi/padInfo', 
                                             json_data={'padCode': device_id})
        if 'code' in test_result and test_result['code'] == 2020:
            print_with_color(f"⚠ 设备ID '{device_id}' 不存在，尝试获取设备列表...", "yellow")
            device_list = list_vmos_devices(access_key_id, secret_access_key, api_host_for_sig)
            if device_list:
                print_with_color(f"✓ 找到 {len(device_list)} 个可用设备: {device_list}", "green")
                print_with_color(f"建议: 请在config.yaml中将VMOS_DEVICE_ID更新为: {device_list[0]}", "yellow")
                device_id = device_list[0]
                controller = VMOSCloudController(device_id, access_key_id, secret_access_key, api_host)
            else:
                print_with_color("✗ 无法获取设备列表", "red")
                return False
        
        # 测试3: 获取设备尺寸
        print_with_color("测试3: 获取设备尺寸...", "blue")
        width, height = controller.get_device_size()
        if width and height:
            print_with_color(f"✓ 设备尺寸: {width}x{height}", "green")
        else:
            print_with_color("✗ 无法获取设备尺寸", "red")
        
        # 测试4: 获取截图
        print_with_color("测试4: 获取截图...", "blue")
        screenshot_path = controller.get_screenshot("test_screenshot", "./")
        if screenshot_path != "ERROR":
            print_with_color(f"✓ 截图保存成功: {screenshot_path}", "green")
        else:
            print_with_color("✗ 截图失败", "red")
        
        print_with_color("", "white")
        print_with_color("=" * 50, "cyan")
        print_with_color("测试完成！", "cyan")
        print_with_color("=" * 50, "cyan")
        return True
    except Exception as e:
        print_with_color(f"✗ 测试失败: {str(e)}", "red")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_vmos_connection()
    sys.exit(0 if success else 1)
