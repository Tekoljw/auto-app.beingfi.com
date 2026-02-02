"""
VMOS Cloud 云手机API控制器
用于通过VMOS Cloud API获取ADB连接信息，然后通过ADB操作云手机
参考文档: https://cloud.vmoscloud.com/vmoscloud/doc/zh/server/OpenAPI.html
"""
import os
import subprocess
import tempfile
import socket
import time
import requests
import base64
import binascii
import hmac
import hashlib
import json
import threading
from datetime import datetime
from typing import Tuple, Optional
from urllib.parse import urlparse, quote
try:
    from .config import load_config
    from .utils import print_with_color
    from .and_controller import AndroidController, execute_adb
except ImportError:
    # 支持作为独立脚本运行
    from config import load_config
    from utils import print_with_color
    from and_controller import AndroidController, execute_adb

configs = load_config()


class VMOSCloudController:
    """VMOS Cloud API控制器，通过获取ADB连接信息，然后使用AndroidController完成所有操作"""
    
    def __init__(self, device_id: str, access_key_id: str, secret_access_key: str, 
                 api_host: str = "api.vmoscloud.com"):
        """
        初始化VMOS Cloud控制器
        
        Args:
            device_id: 云手机设备ID (padCode)
            access_key_id: Access Key ID
            secret_access_key: Secret Access Key
            api_host: API主机地址
        """
        self.device_id = device_id
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        
        # 处理api_host，支持带或不带协议
        if api_host.startswith("http://") or api_host.startswith("https://"):
            # 如果包含协议，提取主机名
            from urllib.parse import urlparse
            parsed = urlparse(api_host)
            self.api_host = parsed.netloc or parsed.path
            self.api_base_url = api_host.rstrip('/')
        else:
            # 如果不包含协议，添加https://
            self.api_host = api_host
            self.api_base_url = f"https://{api_host}"
        
        # 获取ADB连接信息并连接
        self.adb_host = None
        self.adb_port = None
        self.adb_device = None
        self.android_controller = None
        self.ssh_process = None
        self.ssh_key_file = None
        
        # 获取ADB连接信息
        self._get_and_connect_adb()
        
        # 如果ADB连接成功，使用AndroidController
        if self.android_controller:
            self.width, self.height = self.android_controller.width, self.android_controller.height
        else:
            # 如果ADB连接失败，使用默认值
            self.width, self.height = 1080, 1920
            print_with_color("警告: ADB连接失败，使用默认设备尺寸 1080x1920", "yellow")
    
    def _get_and_connect_adb(self):
        """
        获取ADB连接信息并连接到云手机
        通过SSH隧道连接ADB
        """
        print_with_color(f"正在获取设备 {self.device_id} 的ADB连接信息...", "cyan")
        
        # 调用VMOS Cloud API获取ADB连接信息
        # 接口地址: /vcpcloud/api/padApi/adb
        # enable: true-打开 false-关闭（必填）
        result = self._make_request('POST', '/vcpcloud/api/padApi/adb', 
                                  json_data={
                                      'padCode': self.device_id,
                                      'enable': True  # 打开ADB连接
                                  })
        
        # 检查是否成功（code=200）
        if 'code' in result and result['code'] != 200:
            error_msg = result.get('msg', f"错误码: {result['code']}")
            print_with_color(f"获取ADB连接信息失败: {error_msg} (code: {result['code']})", "red")
            return
        
        if 'error' in result:
            print_with_color(f"获取ADB连接信息失败: {result['error']}", "red")
            return
        
        # 解析返回数据
        # 从data字段或直接从result中获取
        adb_info = result.get('data') or result
        
        # 解析SSH连接信息
        # command: SSH连接命令
        # key: SSH连接的密钥
        # adb: 通过SSH连接运行ADB命令的方式
        ssh_command = adb_info.get('command')
        ssh_key = adb_info.get('key')
        adb_info_str = adb_info.get('adb')
        
        if not ssh_command or not ssh_key:
            print_with_color(f"错误: SSH连接信息不完整", "red")
            print_with_color(f"API响应: {json.dumps(adb_info, ensure_ascii=False, indent=2)}", "yellow")
            return
        
        print_with_color(f"SSH命令: {ssh_command}", "cyan")
        
        # VMOS 返回的 key 字段很短（88字符），不是完整 SSH 私钥，可能是密码
        # 尝试 Base64 解码后作为 SSH 密码使用
        import base64
        ssh_password = None
        try:
            decoded_bytes = base64.b64decode(ssh_key)
            ssh_password = decoded_bytes.decode('utf-8')
            print_with_color(f"已解码 SSH 认证信息（长度: {len(ssh_password)}）", "cyan")
        except Exception as e:
            print_with_color(f"解码 key 失败: {e}，尝试直接使用", "yellow")
            ssh_password = ssh_key
        
        # 保存密码到临时文件供 sshpass 使用（若系统有 sshpass）
        self.ssh_key_file = None
        self.ssh_password = ssh_password
        
        # 解析ADB连接信息
        # adb字段可能包含：
        # 1. "host:port" 格式（如 "localhost:9980"）
        # 2. 完整命令字符串（如 "adb connect localhost:9980"）
        # 直接使用返回的adb内容，不使用默认值
        
        if not adb_info_str:
            print_with_color(f"错误: adb字段为空，无法建立ADB连接", "red")
            return
        
        adb_info_str = adb_info_str.strip()
        print_with_color(f"解析adb字段: '{adb_info_str}'", "cyan")
        
        # 解析host:port地址
        remote_host = None
        remote_port = None
        
        # 方法1: 如果是完整命令格式 "adb connect host:port"，提取host:port部分
        if 'adb connect' in adb_info_str.lower():
            # 提取 "connect" 后面的部分
            parts = adb_info_str.split('connect', 1)
            if len(parts) > 1:
                address_part = parts[1].strip()
                if ':' in address_part:
                    # 提取host:port，可能包含空格或其他字符
                    address_part = address_part.split()[0] if address_part.split() else address_part
                    host_port = address_part.split(':')
                    if len(host_port) >= 2:
                        try:
                            remote_host = host_port[0].strip()
                            remote_port = str(int(host_port[1].strip()))
                            print_with_color(f"从命令字符串中解析到ADB地址: {remote_host}:{remote_port}", "cyan")
                        except (ValueError, IndexError) as e:
                            print_with_color(f"错误: 无法从命令字符串中解析地址: {str(e)}", "red")
                            return
        # 方法2: 如果是直接的 "host:port" 格式
        elif ':' in adb_info_str:
            parts = adb_info_str.split(':')
            if len(parts) >= 2:
                try:
                    remote_host = parts[0].strip()
                    remote_port = str(int(parts[1].strip()))
                    print_with_color(f"解析到ADB地址: {remote_host}:{remote_port}", "cyan")
                except (ValueError, IndexError) as e:
                    print_with_color(f"错误: adb字段 '{adb_info_str}' 不是有效的host:port格式: {str(e)}", "red")
                    return
        else:
            # 其他格式，无法解析
            print_with_color(f"错误: adb字段为 '{adb_info_str}'，无法解析为host:port格式", "red")
            return
        
        if not remote_host or not remote_port:
            print_with_color(f"错误: 无法从adb字段中解析出有效的host和port", "red")
            return
        
        # 使用解析出的地址作为本地和远程ADB地址
        # 本地ADB连接使用解析出的地址（通过SSH隧道转发）
        self.adb_host = '127.0.0.1'  # 本地始终使用127.0.0.1
        self.adb_port = remote_port  # 本地端口使用远程端口（通过SSH转发）
        
        # 远程ADB地址（用于SSH端口转发）
        # 注意：如果remote_host是localhost，在SSH隧道中应该使用127.0.0.1
        if remote_host.lower() == 'localhost':
            remote_host_for_tunnel = '127.0.0.1'
        else:
            remote_host_for_tunnel = remote_host
        
        # 建立SSH隧道（端口转发）
        # 从 ssh_command 解析主机、端口、用户名
        print_with_color(f"正在建立SSH隧道（使用密码认证）...", "cyan")
        
        # 解析 SSH 命令：ssh -oHostKeyAlgorithms=+ssh-rsa user@host -p port -L ...
        import re
        ssh_host_match = re.search(r'(\S+)@([\d\.]+)', ssh_command)
        ssh_port_match = re.search(r'-p\s+(\d+)', ssh_command)
        ssh_L_match = re.search(r'-L\s+(\d+):([^:]+):(\d+)', ssh_command)
        
        if not ssh_host_match:
            print_with_color(f"无法从 SSH 命令中解析主机: {ssh_command}", "red")
            return
        
        ssh_user = ssh_host_match.group(1)
        ssh_host = ssh_host_match.group(2)
        ssh_port = int(ssh_port_match.group(1)) if ssh_port_match else 22
        
        if ssh_L_match:
            local_port = int(ssh_L_match.group(1))
            remote_host_tunnel = ssh_L_match.group(2)
            remote_port_tunnel = int(ssh_L_match.group(3))
            # 若远程主机是 adb-proxy，尝试改为 localhost（可能在 SSH 服务器本机）
            if remote_host_tunnel == 'adb-proxy':
                print_with_color(f"检测到 adb-proxy，尝试改用 localhost（SSH 服务器本机）", "yellow")
                remote_host_tunnel = 'localhost'
            print_with_color(f"端口转发: 本地 {local_port} -> 远程 {remote_host_tunnel}:{remote_port_tunnel}", "cyan")
        else:
            local_port = int(self.adb_port)
            remote_host_tunnel = remote_host_for_tunnel
            remote_port_tunnel = int(remote_port)
            print_with_color(f"端口转发: 本地 {local_port} -> 远程 {remote_host_tunnel}:{remote_port_tunnel}", "cyan")
        
        # 使用 paramiko 建立 SSH 隧道（支持密码认证）
        try:
            import paramiko
            import threading
            
            print_with_color(f"使用 paramiko 连接 {ssh_user}@{ssh_host}:{ssh_port} ...", "cyan")
            
            ssh_client = paramiko.SSHClient()
            ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh_client.connect(
                hostname=ssh_host,
                port=ssh_port,
                username=ssh_user,
                password=ssh_password,
                timeout=15
            )
            
            print_with_color(f"SSH 连接成功，正在建立端口转发...", "green")
            
            # 建立端口转发
            transport = ssh_client.get_transport()
            
            def forward_tunnel():
                import select
                server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(('127.0.0.1', local_port))
                server.listen(5)
                print_with_color(f"本地端口 {local_port} 开始监听", "green")
                
                while True:
                    try:
                        client_sock, client_addr = server.accept()
                        channel = transport.open_channel(
                            'direct-tcpip',
                            (remote_host_tunnel, remote_port_tunnel),
                            client_sock.getpeername()
                        )
                        
                        def forward_data(src, dst):
                            try:
                                while True:
                                    data = src.recv(4096)
                                    if not data:
                                        break
                                    dst.sendall(data)
                            except Exception:
                                pass
                            finally:
                                src.close()
                                dst.close()
                        
                        threading.Thread(target=forward_data, args=(client_sock, channel), daemon=True).start()
                        threading.Thread(target=forward_data, args=(channel, client_sock), daemon=True).start()
                    except Exception as e:
                        if 'closed' not in str(e).lower():
                            print_with_color(f"端口转发异常: {e}", "yellow")
                        break
            
            tunnel_thread = threading.Thread(target=forward_tunnel, daemon=True)
            tunnel_thread.start()
            
            self.ssh_client = ssh_client
            self.ssh_process = None
            
            time.sleep(2)
            print_with_color(f"SSH 隧道已建立", "green")
        except ImportError:
            print_with_color("未安装 paramiko 库，请运行: pip install paramiko", "red")
            return
        except Exception as e:
            print_with_color(f"建立 SSH 隧道失败: {str(e)}", "red")
            import traceback
            traceback.print_exc()
            return
        
        # 验证本地端口是否在监听
        print_with_color(f"验证SSH隧道端口是否就绪...", "cyan")
        max_port_check_retries = 5
        port_ready = False
        for i in range(max_port_check_retries):
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex(('127.0.0.1', local_port))
                sock.close()
                if result == 0:
                    port_ready = True
                    print_with_color(f"SSH隧道端口 {local_port} 已就绪", "green")
                    break
                print_with_color(f"等待端口就绪... ({i+1}/{max_port_check_retries})", "yellow")
                time.sleep(2)
            except Exception as e:
                print_with_color(f"检查端口时出错: {str(e)}，重试... ({i+1}/{max_port_check_retries})", "yellow")
                time.sleep(2)
        
        if not port_ready:
            print_with_color(f"警告: 本地端口 {local_port} 未监听", "yellow")
            return
        
        # 构建ADB设备标识（本地端口）
        self.adb_device = f"{self.adb_host}:{self.adb_port}"
        
        print_with_color(f"ADB连接信息: {self.adb_device}", "cyan")
        
        # 端口就绪后再等一小段时间，确保隧道完全可用
        if port_ready:
            print_with_color("SSH隧道已就绪", "cyan")
        
        connect_cmd = f"adb connect {self.adb_device}"
        
        def _adb_connect_output_ok(output_text):
            """根据 adb connect 输出判断是否真正连接成功（部分环境 returncode=0 但输出为错误）"""
            if not output_text:
                return True
            t = output_text.strip().lower()
            if "cannot connect" in t or "无法连接" in t or "拒绝" in t or "refused" in t or "10061" in t:
                return False
            if "connected" in t or "already connected" in t or "已连接" in t:
                return True
            return True  # 无明确错误则视为可能成功
        
        def _run_adb_connect():
            """执行一次 adb connect，返回 (是否执行成功, 输出文本)"""
            try:
                adb_process = subprocess.Popen(
                    connect_cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0,
                )
                stdout, _ = adb_process.communicate(timeout=15)
                out = (stdout or b'').decode('utf-8', errors='replace').strip()
                return adb_process.returncode == 0, out
            except subprocess.TimeoutExpired:
                adb_process.kill()
                return False, "timeout"
            except Exception as e:
                return False, str(e)
        
        # 使用 execute_adb 在当前进程执行，便于正确获取输出并重试
        print_with_color(f"正在连接 ADB: {self.adb_device} ...", "cyan")
        max_connect_retries = 3  # 最多 3 次，每次间隔 2 秒 = 约 6 秒
        connect_ok = False
        last_output = ""
        
        for attempt in range(max_connect_retries):
            result = execute_adb(connect_cmd)
            if result == "ERROR":
                last_output = "execute_adb 返回 ERROR"
                print_with_color(f"ADB 连接尝试 {attempt + 1}/{max_connect_retries}: 执行失败", "yellow")
            else:
                last_output = result
                if _adb_connect_output_ok(result):
                    connect_ok = True
                    print_with_color(f"ADB 连接成功: {result}", "green")
                    break
                print_with_color(f"ADB 连接尝试 {attempt + 1}/{max_connect_retries}: {result}", "yellow")
            if attempt < max_connect_retries - 1:
                wait_sec = 2
                print_with_color(f"等待 {wait_sec} 秒后重试...", "cyan")
                time.sleep(wait_sec)
        
        if not connect_ok:
            print_with_color(f"ADB 连接失败: {last_output}", "red")
            print_with_color("请确认: 1) SSH 隧道已建立 2) 本机已安装 adb 3) 端口未被占用", "yellow")
            return
        
        # 检查连接状态并等待设备在线（最多 5 次 x 2 秒 = 10 秒）
        print_with_color(f"检查ADB设备状态...", "cyan")
        max_device_check_retries = 5
        device_online = False
        
        for i in range(max_device_check_retries):
            devices_cmd = "adb devices"
            devices_result = execute_adb(devices_cmd)
            
            # 检查设备是否在列表中
            if self.adb_device in devices_result or f"{self.adb_host}:{self.adb_port}" in devices_result:
                # 检查设备状态（online/offline/unauthorized）
                device_line = None
                for line in devices_result.split('\n'):
                    if self.adb_device in line or f"{self.adb_host}:{self.adb_port}" in line:
                        device_line = line.strip()
                        break
                
                if device_line:
                    if 'device' in device_line and 'offline' not in device_line:
                        # 设备在线
                        device_online = True
                        print_with_color(f"设备已在线: {device_line}", "green")
                        break
                    elif 'offline' in device_line:
                        print_with_color(f"设备状态为offline，尝试 reconnect... ({i+1}/{max_device_check_retries})", "yellow")
                        # 尝试 adb reconnect 修复 offline 状态
                        reconnect_cmd = f"adb -s {self.adb_device} reconnect"
                        execute_adb(reconnect_cmd)
                        time.sleep(2)
                    elif 'unauthorized' in device_line:
                        print_with_color(f"设备未授权，请检查设备上的授权提示", "yellow")
                        time.sleep(2)
                    else:
                        print_with_color(f"设备状态未知: {device_line}，等待中... ({i+1}/{max_device_check_retries})", "yellow")
                        time.sleep(2)
                else:
                    print_with_color(f"设备在列表中但状态未知，等待中... ({i+1}/{max_device_check_retries})", "yellow")
                    time.sleep(2)
            else:
                # 设备不在列表中，尝试重新连接
                if i < max_device_check_retries - 1:
                    print_with_color(f"设备未在列表中，尝试重新连接... ({i+1}/{max_device_check_retries})", "yellow")
                    reconnect_result = execute_adb(connect_cmd)
                    time.sleep(2)
                else:
                    print_with_color(f"警告: 设备未在列表中: {devices_result}", "yellow")
        
        if device_online:
            print_with_color(f"成功连接到ADB: {self.adb_device}", "green")
        else:
            print_with_color(f"警告: 设备显示为 offline，但尝试继续使用（可能状态显示有延迟）", "yellow")
        
        # 无论状态如何，都尝试创建 AndroidController 并测试能否执行命令
        print_with_color(f"正在初始化 AndroidController 并测试连接...", "cyan")
        try:
            self.android_controller = AndroidController(self.adb_device)
            # 测试能否执行命令
            test_result = execute_adb(f"adb -s {self.adb_device} shell echo test")
            if test_result and test_result != "ERROR" and "test" in test_result:
                print_with_color(f"ADB 命令测试成功，设备可用", "green")
            else:
                print_with_color(f"ADB 命令测试失败: {test_result}，但继续尝试", "yellow")
        except Exception as e:
            print_with_color(f"初始化 AndroidController 时出错: {e}，但继续尝试", "yellow")
            self.android_controller = AndroidController(self.adb_device)
    
    def _generate_signature(self, method: str, path: str, query_params: dict = None, 
                           headers: dict = None, body: str = "") -> dict:
        """
        生成VMOS Cloud API签名
        根据VMOS Cloud API官方demo实现HMAC-SHA256签名算法
        
        Args:
            method: HTTP方法
            path: 请求路径
            query_params: 查询参数
            headers: 请求头
            body: 请求体
            
        Returns:
            包含签名信息的headers字典
        """
        import binascii
        
        # 生成时间戳 (UTC时间，格式: YYYYMMDDTHHMMSSZ)
        now = datetime.utcnow()
        x_date = now.strftime("%Y%m%dT%H%M%SZ")
        short_x_date = x_date[:8]  # 短请求时间，例如："20240101"
        
        # 确保host只包含域名，不包含协议
        host_name = self.api_host.lower()
        if host_name.startswith('http://'):
            host_name = host_name[7:]
        elif host_name.startswith('https://'):
            host_name = host_name[8:]
        
        # content-type需要包含charset
        content_type = "application/json;charset=UTF-8"
        signed_headers = "content-type;host;x-content-sha256;x-date"
        
        # 计算请求体的SHA256（根据demo，body应该是JSON字符串）
        if body:
            x_content_sha256 = hashlib.sha256(body.encode('utf-8')).hexdigest()
        else:
            x_content_sha256 = hashlib.sha256(b'').hexdigest()
        
        # 构建canonical_string_builder（根据demo格式）
        canonical_string_builder = (
            f"host:{host_name}\n"
            f"x-date:{x_date}\n"
            f"content-type:{content_type}\n"
            f"signedHeaders:{signed_headers}\n"
            f"x-content-sha256:{x_content_sha256}"
        )
        
        # 计算canonical_string_builder的SHA-256哈希值
        hash_sha256 = hashlib.sha256(canonical_string_builder.encode('utf-8')).hexdigest()
        
        # 根据demo，service固定为"armcloud-paas"
        # service用于：
        # 1. 构建credential_scope（用于StringToSign）
        # 2. 签名密钥派生（第二次HMAC）
        service = "armcloud-paas"  # 固定值，与demo一致
        
        # 构建credential_scope（虽然VMOS的Authorization头不包含，但可能用于StringToSign）
        credential_scope = f"{short_x_date}/{service}/request"
        
        # 构建StringToSign
        # 根据demo，StringToSign包含credential_scope
        string_to_sign = (
            "HMAC-SHA256\n" +
            x_date + "\n" +
            credential_scope + "\n" +
            hash_sha256
        )
        
        # 派生签名密钥（根据demo，使用三次HMAC）
        # 第一次hmacSHA256: HMAC-SHA256(SK, short_date)
        first_hmac = hmac.new(self.secret_access_key.encode('utf-8'), digestmod=hashlib.sha256)
        first_hmac.update(short_x_date.encode('utf-8'))
        first_hmac_result = first_hmac.digest()
        
        # 第二次hmacSHA256: HMAC-SHA256(first_result, service)
        second_hmac = hmac.new(first_hmac_result, digestmod=hashlib.sha256)
        second_hmac.update(service.encode('utf-8'))
        second_hmac_result = second_hmac.digest()
        
        # 第三次hmacSHA256: HMAC-SHA256(second_result, 'request')
        signing_key = hmac.new(second_hmac_result, b'request', digestmod=hashlib.sha256).digest()
        
        # 使用signing_key和string_to_sign计算HMAC-SHA256
        signature_bytes = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).digest()
        
        # 将HMAC-SHA256的结果转换为十六进制编码的字符串
        signature = binascii.hexlify(signature_bytes).decode()
        
        # 构建Authorization头（根据demo格式）
        authorization = (
            f"HMAC-SHA256 Credential={self.access_key_id}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )
        
        # 返回包含签名的headers（根据demo格式）
        request_headers = {
            'content-type': content_type,
            'x-date': x_date,
            'x-host': host_name,
            'x-content-sha256': x_content_sha256,
            'authorization': authorization
        }
        
        # 调试信息
        DEBUG_SIGNATURE = True
        if DEBUG_SIGNATURE:
            print_with_color("=" * 60, "cyan")
            print_with_color("DEBUG - Signature Generation (VMOS Format)", "cyan")
            print_with_color("=" * 60, "cyan")
            print_with_color(f"Method: {method.upper()}", "yellow")
            print_with_color(f"Path: {path}", "yellow")
            print_with_color(f"Host: {host_name}", "yellow")
            print_with_color(f"X-Date: {x_date}", "yellow")
            print_with_color(f"Content-Type: {content_type}", "yellow")
            print_with_color(f"Body Hash (x-content-sha256): {x_content_sha256}", "yellow")
            print_with_color(f"\nCanonical String Builder:\n{canonical_string_builder}", "yellow")
            print_with_color(f"\nCanonical Hash: {hash_sha256}", "yellow")
            print_with_color(f"\nString to Sign:\n{string_to_sign}", "yellow")
            print_with_color(f"\nSignature: {signature}", "yellow")
            print_with_color(f"\nAuthorization: {authorization}", "yellow")
            print_with_color("=" * 60, "cyan")
        
        return request_headers
    
    def _make_request(self, method: str, endpoint: str, params: dict = None, 
                     json_data: dict = None, data: dict = None) -> dict:
        """
        发送HTTP请求到VMOS Cloud API
        
        Args:
            method: HTTP方法 (GET, POST, PUT等)
            endpoint: API端点路径
            params: URL查询参数
            json_data: JSON请求体
            data: Form数据请求体
            
        Returns:
            API响应结果
        """
        # 构建完整URL
        path = endpoint if endpoint.startswith('/') else f'/{endpoint}'
        url = f"{self.api_base_url}{path}"
        
        # 准备请求体（根据demo，使用separators去除空格）
        body = ""
        if json_data:
            # 根据demo，使用separators=(',', ':')去除空格
            body = json.dumps(json_data, separators=(',', ':'), ensure_ascii=False)
            # 调试：打印请求参数
            DEBUG_REQUEST = True
            if DEBUG_REQUEST:
                print_with_color(f"请求参数 ({endpoint}): {body}", "cyan")
        elif data:
            body = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        
        # 生成签名
        headers = self._generate_signature(method, path, params, None, body)
        
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, params=params, timeout=30)
            elif method.upper() == 'POST':
                # 使用data参数发送body，确保与签名时使用的body一致
                if body:
                    response = requests.post(url, headers=headers, params=params, 
                                           data=body.encode('utf-8'), timeout=30)
                else:
                    response = requests.post(url, headers=headers, params=params, 
                                           timeout=30)
            elif method.upper() == 'PUT':
                response = requests.put(url, headers=headers, params=params, 
                                      json=json_data, timeout=30)
            else:
                return {"error": f"Unsupported HTTP method: {method}"}
            
            response.raise_for_status()
            
            # 解析响应
            if response.content:
                try:
                    result = response.json()
                    # 调试：打印响应内容（前500字符）
                    DEBUG_RESPONSE = True
                    if DEBUG_RESPONSE:
                        import json as json_module
                        result_str = json_module.dumps(result, ensure_ascii=False, indent=2)
                        if len(result_str) > 500:
                            result_str = result_str[:500] + "..."
                        print_with_color(f"API响应 ({endpoint}):\n{result_str}", "cyan")
                    return result
                except Exception as e:
                    print_with_color(f"解析JSON响应失败: {str(e)}, 原始响应: {response.text[:200]}", "yellow")
                    return {"status": "success", "raw": response.text}
            else:
                return {"status": "success"}
            
        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP错误: {e.response.status_code}"
            try:
                error_detail = e.response.json()
                error_msg += f" - {error_detail}"
            except:
                error_msg += f" - {e.response.text}"
            print_with_color(f"API请求失败: {endpoint}, {error_msg}", "red")
            return {"error": error_msg}
        except requests.exceptions.RequestException as e:
            print_with_color(f"API请求失败: {endpoint}, 错误: {str(e)}", "red")
            return {"error": str(e)}
    
    def get_device_size(self) -> Tuple[int, int]:
        """
        获取设备屏幕尺寸
        
        Returns:
            (width, height) 元组
        """
        if self.android_controller:
            return self.android_controller.get_device_size()
        return 1080, 1920
    
    def get_screenshot(self, prefix: str, save_dir: str) -> str:
        """
        获取屏幕截图
        通过ADB获取截图
        
        Args:
            prefix: 文件名前缀
            save_dir: 保存目录
            
        Returns:
            截图文件路径，失败返回 "ERROR"
        """
        if self.android_controller:
            return self.android_controller.get_screenshot(prefix, save_dir)
        print_with_color("错误: ADB未连接，无法获取截图", "red")
        return "ERROR"
    
    def get_xml(self, prefix: str, save_dir: str) -> str:
        """
        获取UI层级XML
        通过ADB获取UI层级
        
        Args:
            prefix: 文件名前缀
            save_dir: 保存目录
            
        Returns:
            XML文件路径，失败返回 "ERROR"
        """
        if self.android_controller:
            return self.android_controller.get_xml(prefix, save_dir)
        print_with_color("错误: ADB未连接，无法获取XML", "red")
        return "ERROR"
    
    def back(self) -> str:
        """按返回键"""
        if self.android_controller:
            return self.android_controller.back()
        print_with_color("错误: ADB未连接，无法执行返回操作", "red")
        return "ERROR"
    
    def tap(self, x: int, y: int) -> str:
        """
        点击坐标
        通过ADB执行点击操作
        
        Args:
            x: X坐标
            y: Y坐标
        """
        if self.android_controller:
            return self.android_controller.tap(x, y)
        print_with_color("错误: ADB未连接，无法执行点击操作", "red")
        return "ERROR"
    
    def text(self, input_str: str) -> str:
        """
        输入文本
        通过ADB执行文本输入操作
        
        Args:
            input_str: 要输入的文本
        """
        if self.android_controller:
            return self.android_controller.text(input_str)
        print_with_color("错误: ADB未连接，无法执行文本输入操作", "red")
        return "ERROR"
    
    def long_press(self, x: int, y: int, duration: int = 1000) -> str:
        """
        长按坐标
        通过ADB执行长按操作
        
        Args:
            x: X坐标
            y: Y坐标
            duration: 持续时间（毫秒）
        """
        if self.android_controller:
            return self.android_controller.long_press(x, y, duration)
        print_with_color("错误: ADB未连接，无法执行长按操作", "red")
        return "ERROR"
    
    def swipe(self, x: int, y: int, direction: str, dist: str = "medium", quick: bool = False) -> str:
        """
        滑动操作
        通过ADB执行滑动操作
        
        Args:
            x: 起始X坐标
            y: 起始Y坐标
            direction: 滑动方向 (up/down/left/right)
            dist: 滑动距离 (short/medium/long)
            quick: 是否快速滑动
        """
        if self.android_controller:
            return self.android_controller.swipe(x, y, direction, dist, quick)
        print_with_color("错误: ADB未连接，无法执行滑动操作", "red")
        return "ERROR"
    
    def swipe_precise(self, start: Tuple[int, int], end: Tuple[int, int], duration: int = 400) -> str:
        """
        精确滑动
        通过ADB执行精确滑动操作
        
        Args:
            start: 起始坐标 (x, y)
            end: 结束坐标 (x, y)
            duration: 滑动持续时间（毫秒）
        """
        if self.android_controller:
            return self.android_controller.swipe_precise(start, end, duration)
        print_with_color("错误: ADB未连接，无法执行精确滑动操作", "red")
        return "ERROR"
    
    def __del__(self):
        """
        清理资源：关闭SSH隧道，删除临时密钥文件
        """
        self.cleanup()
    
    def cleanup(self):
        """
        清理资源：关闭SSH隧道
        """
        # 关闭 paramiko SSH 客户端
        if hasattr(self, 'ssh_client') and self.ssh_client:
            try:
                self.ssh_client.close()
            except:
                pass
            self.ssh_client = None
        
        # 关闭SSH隧道进程（若用的是 subprocess）
        if hasattr(self, 'ssh_process') and self.ssh_process:
            try:
                self.ssh_process.terminate()
                self.ssh_process.wait(timeout=5)
            except:
                try:
                    self.ssh_process.kill()
                except:
                    pass
            self.ssh_process = None
        
        # 删除临时SSH密钥文件
        if self.ssh_key_file and os.path.exists(self.ssh_key_file):
            try:
                os.remove(self.ssh_key_file)
            except:
                pass
            self.ssh_key_file = None


def list_vmos_devices(access_key_id: str, secret_access_key: str, 
                     api_host: str = "api.vmoscloud.com") -> list:
    """
    列出所有可用的VMOS Cloud设备
    
    Args:
        access_key_id: Access Key ID
        secret_access_key: Secret Access Key
        api_host: API主机地址
        
    Returns:
        设备ID列表
    """
    controller = VMOSCloudController("", access_key_id, secret_access_key, api_host)
    
    # 调用VMOS Cloud API: 查询所有已订购实例的列表信息
    # 根据VMOS Cloud API文档，该接口可能需要分页参数
    # 根据文档，infos接口通常需要分页参数：page（页码）和pageSize（每页数量）
    # 先尝试带分页参数（获取第一页，每页100条）
    result = controller._make_request('POST', '/vcpcloud/api/padApi/infos', 
                                     json_data={'page': 1, 'pageSize': 100})
    
    # 如果失败，尝试不带参数（某些情况下可能不需要参数）
    if 'code' in result and result.get('code') != 200:
        print_with_color("尝试不带参数获取设备列表...", "yellow")
        result = controller._make_request('POST', '/vcpcloud/api/padApi/infos', 
                                         json_data={})
    
    device_list = []
    
    # 检查错误
    if 'error' in result:
        print_with_color(f"获取设备列表失败: {result['error']}", "red")
        return device_list
    
    # 检查API返回的错误码（code=200表示成功）
    if 'code' in result:
        code = result['code']
        if code != 200:
            error_msg = result.get('msg', f"错误码: {code}")
            print_with_color(f"获取设备列表失败: {error_msg} (code: {code})", "red")
            # 如果是系统繁忙，提示重试
            if code == 500:
                if 'busy' in error_msg.lower():
                    print_with_color("提示: 系统繁忙，请稍后重试", "yellow")
                else:
                    print_with_color("提示: 系统异常，请稍后重试或联系技术支持", "yellow")
            return device_list
        if 'data' in result:
            data = result['data']
            if isinstance(data, list):
                # data是数组格式
                for device in data:
                    device_id = device.get('padCode') or device.get('deviceId') or device.get('id', '')
                    if device_id:
                        device_list.append(device_id)
            elif isinstance(data, dict):
                # data是字典格式，可能包含list或pageData字段
                if 'pageData' in data:
                    # 分页格式：data.pageData是设备数组
                    for device in data['pageData']:
                        device_id = device.get('padCode') or device.get('deviceId') or device.get('id', '')
                        if device_id:
                            device_list.append(device_id)
                elif 'list' in data:
                    # 列表格式：data.list是设备数组
                    for device in data['list']:
                        device_id = device.get('padCode') or device.get('deviceId') or device.get('id', '')
                        if device_id:
                            device_list.append(device_id)
        elif 'list' in result:
            # 直接在result中的list字段
            for device in result['list']:
                device_id = device.get('padCode') or device.get('deviceId') or device.get('id', '')
                if device_id:
                    device_list.append(device_id)
    
    return device_list

