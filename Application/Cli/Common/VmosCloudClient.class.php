<?php
namespace Cli\Common;

/**
 * VMOS Cloud OpenAPI 客户端（ADB 开关、异步 ADB、任务查询）
 * 文档：https://cloud.vmoscloud.com/vmoscloud/doc/zh/server/OpenAPI.html
 * 鉴权说明：https://cloud.vmoscloud.com/vmoscloud/doc/zh/server/example.html
 *
 * 配置（ThinkPHP config.php）：
 * - VMOS_CLOUD_API_BASE           默认 https://api.vmoscloud.com
 * - VMOS_CLOUD_ACCESS_KEY_ID      Access Key ID（AK），控制台 开发者 → API
 * - VMOS_CLOUD_SECRET_ACCESS_KEY  Secret Access Key（SK）
 * 可选别名：VMOS_CLOUD_AK / VMOS_CLOUD_SK
 *
 * 若未配置 AK/SK，可回退（旧方式）：VMOS_CLOUD_ACCESS_TOKEN 或 VMOS_CLOUD_BEARER_TOKEN（Bearer，非官方示例主路径）
 */
class VmosCloudClient {

    protected $baseUrl;
    protected $signHost;
    protected $accessKeyId;
    protected $secretAccessKey;
    protected $token;

    public function __construct() {
        $this->baseUrl = rtrim((string)(C('VMOS_CLOUD_API_BASE') ?: 'https://api.vmoscloud.com'), '/');
        $host = parse_url($this->baseUrl, PHP_URL_HOST);
        $this->signHost = $host ? (string)$host : 'api.vmoscloud.com';

        $this->accessKeyId = trim((string)(C('VMOS_CLOUD_ACCESS_KEY_ID') ?: C('VMOS_CLOUD_AK') ?: ''));
        $this->secretAccessKey = trim((string)(C('VMOS_CLOUD_SECRET_ACCESS_KEY') ?: C('VMOS_CLOUD_SK') ?: ''));
        $this->token = trim((string)(C('VMOS_CLOUD_ACCESS_TOKEN') ?: C('VMOS_CLOUD_BEARER_TOKEN') ?: ''));
    }

    /**
     * @param string $path 如 /vcpcloud/api/padApi/asyncCmd
     * @param array  $body
     * @return array|null [ 'code'=>200, 'data'=>..., 'msg'=>... ] 或 null
     */
    public function postJson($path, $body) {
        $url = $this->baseUrl . $path;
        $payload = json_encode($body, JSON_UNESCAPED_UNICODE);

        if ($this->accessKeyId !== '' && $this->secretAccessKey !== '') {
            $signer = new VmosCloudSigner($this->accessKeyId, $this->secretAccessKey);
            $headers = $signer->signPost($this->signHost, $payload);
        } elseif ($this->token !== '') {
            $headers = [
                'Content-Type: application/json',
                'Authorization: Bearer ' . $this->token,
            ];
        } else {
            $headers = ['Content-Type: application/json;charset=UTF-8'];
        }

        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_POST           => true,
            CURLOPT_POSTFIELDS     => $payload,
            CURLOPT_HTTPHEADER     => $headers,
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => 120,
        ]);
        $resp = curl_exec($ch);
        $err = curl_error($ch);
        curl_close($ch);
        if ($err || $resp === false) {
            return null;
        }
        $data = json_decode($resp, true);
        return is_array($data) ? $data : null;
    }

    /** 开启/关闭 ADB：openStatus 1 开启 */
    public function openOnlineAdb(array $padCodes, $openStatus = 1) {
        return $this->postJson('/vcpcloud/api/padApi/openOnlineAdb', [
            'padCodes'   => $padCodes,
            'openStatus' => (int)$openStatus,
        ]);
    }

    /** 获取 ADB 连接信息 */
    public function getAdbInfo($padCode, $enable = true) {
        return $this->postJson('/vcpcloud/api/padApi/adb', [
            'padCode' => $padCode,
            'enable'  => (bool)$enable,
        ]);
    }

    /** 异步执行 ADB shell 命令（多条用分号分隔） */
    public function asyncCmd(array $padCodes, $scriptContent) {
        return $this->postJson('/vcpcloud/api/padApi/asyncCmd', [
            'padCodes'      => $padCodes,
            'scriptContent' => (string)$scriptContent,
        ]);
    }

    /** 查询实例操作任务详情 */
    public function padTaskDetail(array $taskIds) {
        return $this->postJson('/vcpcloud/api/padApi/padTaskDetail', [
            'taskIds' => $taskIds,
        ]);
    }

    /**
     * 轮询任务直到完成或超时
     * @param int   $taskId
     * @param int   $maxWaitSec
     * @param float $intervalSec
     * @return array|null 最后一帧 data 中的单条任务详情或 null
     */
    public function waitPadTask($taskId, $maxWaitSec = 180, $intervalSec = 2.0) {
        $deadline = microtime(true) + $maxWaitSec;
        while (microtime(true) < $deadline) {
            $res = $this->padTaskDetail([(int)$taskId]);
            if ($res === null || !isset($res['code']) || (int)$res['code'] !== 200) {
                usleep((int)($intervalSec * 1000000));
                continue;
            }
            $data = isset($res['data']) ? $res['data'] : [];
            if (!is_array($data) || empty($data)) {
                usleep((int)($intervalSec * 1000000));
                continue;
            }
            $row = $data[0];
            $status = isset($row['taskStatus']) ? $row['taskStatus'] : null;
            if ($status === 3 || $status === '3') {
                return $row;
            }
            if (in_array($status, [-1, -2, -3, -4], true)) {
                return $row;
            }
            usleep((int)($intervalSec * 1000000));
        }
        return null;
    }

    /**
     * 执行一段 ADB 脚本并等待完成（依赖 asyncCmd 返回的 taskId）
     * @return array [ 'ok'=>bool, 'detail'=>..., 'error'=>string ]
     */
    public function execAdbAndWait($padCode, $scriptContent, $maxWaitSec = 180) {
        $res = $this->asyncCmd([$padCode], $scriptContent);
        if ($res === null || !isset($res['code']) || (int)$res['code'] !== 200) {
            return ['ok' => false, 'error' => 'asyncCmd_failed', 'raw' => $res];
        }
        $data = isset($res['data']) ? $res['data'] : [];
        if (!is_array($data) || empty($data[0])) {
            return ['ok' => false, 'error' => 'asyncCmd_no_data', 'raw' => $res];
        }
        $taskId = isset($data[0]['taskId']) ? (int)$data[0]['taskId'] : 0;
        if ($taskId <= 0) {
            return ['ok' => false, 'error' => 'asyncCmd_no_taskId', 'raw' => $res];
        }
        $detail = $this->waitPadTask($taskId, $maxWaitSec);
        if ($detail === null) {
            return ['ok' => false, 'error' => 'task_timeout', 'taskId' => $taskId];
        }
        $st = isset($detail['taskStatus']) ? $detail['taskStatus'] : null;
        $ok = ($st === 3 || $st === '3');
        return ['ok' => $ok, 'detail' => $detail, 'taskId' => $taskId];
    }
}
