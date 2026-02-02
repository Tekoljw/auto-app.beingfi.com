import re
from abc import abstractmethod
from typing import List
from http import HTTPStatus

import requests
import dashscope
from dashscope.common.error import UploadFileException

from utils import print_with_color, encode_image


class BaseModel:
    def __init__(self):
        pass

    @abstractmethod
    def get_model_response(self, prompt: str, images: List[str]) -> (bool, str):
        pass


class OpenAIModel(BaseModel):
    def __init__(self, base_url: str, api_key: str, model: str, temperature: float, max_tokens: int):
        super().__init__()
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def get_model_response(self, prompt: str, images: List[str]) -> (bool, str):
        content = [
            {
                "type": "text",
                "text": prompt
            }
        ]
        for img in images:
            base64_img = encode_image(img)
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64_img}"
                }
            })
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }
        response = requests.post(self.base_url, headers=headers, json=payload).json()
        if "error" not in response:
            usage = response["usage"]
            prompt_tokens = usage["prompt_tokens"]
            completion_tokens = usage["completion_tokens"]
            print_with_color(f"Request cost is "
                             f"${'{0:.2f}'.format(prompt_tokens / 1000 * 0.01 + completion_tokens / 1000 * 0.03)}",
                             "yellow")
        else:
            return False, response["error"]["message"]
        return True, response["choices"][0]["message"]["content"]


class QwenModel(BaseModel):
    def __init__(self, api_key: str, model: str):
        super().__init__()
        self.model = model
        dashscope.api_key = api_key

    def get_model_response(self, prompt: str, images: List[str]) -> (bool, str):
        content = [{
            "text": prompt
        }]
        for img in images:
            img_path = f"file://{img}"
            content.append({
                "image": img_path
            })
        messages = [
            {
                "role": "user",
                "content": content
            }
        ]
        try:
            response = dashscope.MultiModalConversation.call(model=self.model, messages=messages)
            if response.status_code == HTTPStatus.OK:
                return True, response.output.choices[0].message.content[0]["text"]
            else:
                error_msg = response.message
                if "InvalidApiKey" in error_msg or "Invalid API-key" in error_msg:
                    error_msg = f"API key 无效，请检查 config.yaml 中的 DASHSCOPE_API_KEY 是否正确。原始错误: {error_msg}"
                return False, error_msg
        except UploadFileException as e:
            error_msg = str(e)
            if "InvalidApiKey" in error_msg or "Invalid API-key" in error_msg:
                error_msg = f"API key 无效，请检查 config.yaml 中的 DASHSCOPE_API_KEY 是否正确。原始错误: {error_msg}"
            print_with_color(f"ERROR: {error_msg}", "red")
            return False, error_msg
        except Exception as e:
            error_msg = f"调用千问模型时发生错误: {str(e)}"
            print_with_color(f"ERROR: {error_msg}", "red")
            return False, error_msg


class DeepSeekModel(BaseModel):
    def __init__(self, base_url: str, api_key: str, model: str, temperature: float, max_tokens: int):
        super().__init__()
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens

    def get_model_response(self, prompt: str, images: List[str]) -> (bool, str):
        content = [
            {
                "type": "text",
                "text": prompt
            }
        ]
        for img in images:
            base64_img = encode_image(img)
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{base64_img}"
                }
            })
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens
        }
        try:
            response = requests.post(self.base_url, headers=headers, json=payload)
            # 先尝试解析响应，即使状态码不是 200
            try:
                response_json = response.json()
            except:
                response_json = {}
            
            if response.status_code != 200:
                error_detail = ""
                if response_json and "error" in response_json:
                    error_detail = response_json["error"].get("message", "")
                    error_code = response_json["error"].get("code", "")
                    if error_code:
                        error_detail = f"[{error_code}] {error_detail}"
                else:
                    error_detail = response.text[:500] if response.text else "无详细错误信息"
                
                error_msg = f"DeepSeek API 返回错误 (状态码: {response.status_code}): {error_detail}"
                print_with_color(f"ERROR: {error_msg}", "red")
                if response.status_code == 400:
                    print_with_color(f"提示: DeepSeek 的 {self.model} 模型可能不支持视觉输入（图像）。", "yellow")
                    print_with_color(f"建议: 1) 检查是否有支持视觉的 DeepSeek 模型可用", "yellow")
                    print_with_color(f"      2) 或者切换到支持视觉的模型，如 Qwen-VL (在 config.yaml 中设置 MODEL: 'Qwen')", "yellow")
                return False, error_msg
            
            if "error" not in response_json:
                if "usage" in response_json:
                    usage = response_json["usage"]
                    prompt_tokens = usage.get("prompt_tokens", 0)
                    completion_tokens = usage.get("completion_tokens", 0)
                    print_with_color(f"Request tokens: prompt={prompt_tokens}, completion={completion_tokens}", "yellow")
                return True, response_json["choices"][0]["message"]["content"]
            else:
                error_msg = response_json["error"].get("message", "Unknown error")
                if "Invalid" in error_msg or "API key" in error_msg:
                    error_msg = f"API key 无效，请检查 config.yaml 中的 DEEPSEEK_API_KEY 是否正确。原始错误: {error_msg}"
                return False, error_msg
        except requests.exceptions.RequestException as e:
            error_msg = f"调用 DeepSeek 模型时发生网络错误: {str(e)}"
            print_with_color(f"ERROR: {error_msg}", "red")
            return False, error_msg
        except Exception as e:
            error_msg = f"调用 DeepSeek 模型时发生错误: {str(e)}"
            print_with_color(f"ERROR: {error_msg}", "red")
            return False, error_msg


def parse_explore_rsp(rsp):
    try:
        observation = re.findall(r"Observation: (.*?)$", rsp, re.MULTILINE)[0]
        think = re.findall(r"Thought: (.*?)$", rsp, re.MULTILINE)[0]
        act = re.findall(r"Action: (.*?)$", rsp, re.MULTILINE)[0]
        last_act = re.findall(r"Summary: (.*?)$", rsp, re.MULTILINE)[0]
        print_with_color("Observation:", "yellow")
        print_with_color(observation, "magenta")
        print_with_color("Thought:", "yellow")
        print_with_color(think, "magenta")
        print_with_color("Action:", "yellow")
        print_with_color(act, "magenta")
        print_with_color("Summary:", "yellow")
        print_with_color(last_act, "magenta")
        if "FINISH" in act:
            return ["FINISH"]
        act_name = act.split("(")[0]
        if act_name == "tap":
            area = int(re.findall(r"tap\((.*?)\)", act)[0])
            return [act_name, area, last_act]
        elif act_name == "text":
            input_str = re.findall(r"text\((.*?)\)", act)[0][1:-1]
            return [act_name, input_str, last_act]
        elif act_name == "long_press":
            area = int(re.findall(r"long_press\((.*?)\)", act)[0])
            return [act_name, area, last_act]
        elif act_name == "swipe":
            params = re.findall(r"swipe\((.*?)\)", act)[0]
            area, swipe_dir, dist = params.split(",")
            area = int(area)
            swipe_dir = swipe_dir.strip()[1:-1]
            dist = dist.strip()[1:-1]
            return [act_name, area, swipe_dir, dist, last_act]
        elif act_name == "grid":
            return [act_name]
        else:
            print_with_color(f"ERROR: Undefined act {act_name}!", "red")
            return ["ERROR"]
    except Exception as e:
        print_with_color(f"ERROR: an exception occurs while parsing the model response: {e}", "red")
        print_with_color(rsp, "red")
        return ["ERROR"]


def parse_grid_rsp(rsp):
    try:
        observation = re.findall(r"Observation: (.*?)$", rsp, re.MULTILINE)[0]
        think = re.findall(r"Thought: (.*?)$", rsp, re.MULTILINE)[0]
        act = re.findall(r"Action: (.*?)$", rsp, re.MULTILINE)[0]
        last_act = re.findall(r"Summary: (.*?)$", rsp, re.MULTILINE)[0]
        print_with_color("Observation:", "yellow")
        print_with_color(observation, "magenta")
        print_with_color("Thought:", "yellow")
        print_with_color(think, "magenta")
        print_with_color("Action:", "yellow")
        print_with_color(act, "magenta")
        print_with_color("Summary:", "yellow")
        print_with_color(last_act, "magenta")
        if "FINISH" in act:
            return ["FINISH"]
        act_name = act.split("(")[0]
        if act_name == "tap":
            params = re.findall(r"tap\((.*?)\)", act)[0].split(",")
            area = int(params[0].strip())
            subarea = params[1].strip()[1:-1]
            return [act_name + "_grid", area, subarea, last_act]
        elif act_name == "long_press":
            params = re.findall(r"long_press\((.*?)\)", act)[0].split(",")
            area = int(params[0].strip())
            subarea = params[1].strip()[1:-1]
            return [act_name + "_grid", area, subarea, last_act]
        elif act_name == "swipe":
            params = re.findall(r"swipe\((.*?)\)", act)[0].split(",")
            start_area = int(params[0].strip())
            start_subarea = params[1].strip()[1:-1]
            end_area = int(params[2].strip())
            end_subarea = params[3].strip()[1:-1]
            return [act_name + "_grid", start_area, start_subarea, end_area, end_subarea, last_act]
        elif act_name == "grid":
            return [act_name]
        else:
            print_with_color(f"ERROR: Undefined act {act_name}!", "red")
            return ["ERROR"]
    except Exception as e:
        print_with_color(f"ERROR: an exception occurs while parsing the model response: {e}", "red")
        print_with_color(rsp, "red")
        return ["ERROR"]


def parse_reflect_rsp(rsp):
    try:
        decision = re.findall(r"Decision: (.*?)$", rsp, re.MULTILINE)[0]
        think = re.findall(r"Thought: (.*?)$", rsp, re.MULTILINE)[0]
        print_with_color("Decision:", "yellow")
        print_with_color(decision, "magenta")
        print_with_color("Thought:", "yellow")
        print_with_color(think, "magenta")
        if decision == "INEFFECTIVE":
            return [decision, think]
        elif decision == "BACK" or decision == "CONTINUE" or decision == "SUCCESS":
            doc = re.findall(r"Documentation: (.*?)$", rsp, re.MULTILINE)[0]
            print_with_color("Documentation:", "yellow")
            print_with_color(doc, "magenta")
            return [decision, think, doc]
        else:
            print_with_color(f"ERROR: Undefined decision {decision}!", "red")
            return ["ERROR"]
    except Exception as e:
        print_with_color(f"ERROR: an exception occurs while parsing the model response: {e}", "red")
        print_with_color(rsp, "red")
        return ["ERROR"]
