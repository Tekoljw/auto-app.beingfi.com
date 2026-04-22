<?php
namespace Cli\Controller;

use Cli\Common\ExchangeCommon;
use Cli\Common\VmosCloudClient;

/**
 * 云手机 APP 自动交换：通道顺序调度、VMOS OpenAPI、文档与 Web 一致；AI 使用通义千问（DashScope 兼容模式）
 *
 * 数据：
 *   tw_paytype_config.is_web = 2 为 APP 通道
 *   tw_payparams_list.vmosid = 云机实例编号（padCode）；vmosapp = 分身标识
 *
 * CLI：php index.php Cli AutoVmosExchange index
 * 配置：VMOS_CLOUD_API_BASE、VMOS_CLOUD_ACCESS_KEY_ID、VMOS_CLOUD_SECRET_ACCESS_KEY（官方 AK/SK 签名，见 VMOS 使用指南）、AUTO_VMOS_EXCHANGE_QWEN_API_KEY、AUTO_WEB_EXCHANGE_BASE_URL（与 Web 共用 orderQueren 等）
 */
class AutoVmosExchangeController extends ExchangeBaseController {

    /** 千问 DashScope 兼容 OpenAI（与 DeepSeek 路径一致） */
    const QWEN_COMPAT_API = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions';
    const QWEN_MODEL_DEFAULT = 'qwen-turbo';

    protected $nodeScriptPath;
    /** [ {username,password,channelid,channel_type,payparams_id,appid,vmosid,vmosapp}, ... ] */
    protected $accounts = [];

    public function __construct() {
        parent::__construct();
        $root = defined('ROOT_PATH') ? ROOT_PATH : dirname(dirname(dirname(dirname(__FILE__))));
        $this->opsDocPath = $root . '/Runtime/auto_vmos_exchange_ops.json';
        $this->channelDocDir = $root . '/Public/AppAutoScriptDoc';
        $this->runtimePath = $root . '/Runtime';
        $this->nodeScriptPath = $root . '/VmosExecutor/run_android_vm.js';
        $this->accounts = $this->_getAppAccounts();
    }

    /**
     * 仅 APP 渠道 is_web=2；账号展开 channelid；需 vmosid 非空
     */
    protected function _getAppAccounts() {
        $prefix = defined('C') ? C('DB_PREFIX') : 'tw_';
        $configTable = $prefix . 'paytype_config';
        $paramsTable = $prefix . 'payparams_list';
        try {
            $channelRows = M()->table($configTable)->where(['is_web' => 2])->field('channelid,channel_type')->select();
            if (!is_array($channelRows) || empty($channelRows)) {
                return [];
            }
            $channels = [];
            foreach ($channelRows as $c) {
                $cid = isset($c['channelid']) ? (int)$c['channelid'] : 0;
                if ($cid > 0) {
                    $channelType = isset($c['channel_type']) ? (int)$c['channel_type'] : 2;
                    $channels[$cid] = ['channelid' => $cid, 'channel_type' => $channelType];
                }
            }
            if (empty($channels)) {
                return [];
            }
            $accountRows = M()->table($paramsTable)->field('id,channelid,login_account,login_password,appid,vmosid,vmosapp')->select();
            if (!is_array($accountRows) || empty($accountRows)) {
                return [];
            }
            $list = [];
            foreach ($accountRows as $p) {
                $accountChannelIds = [];
                $raw = isset($p['channelid']) ? trim((string)$p['channelid']) : '';
                if ($raw !== '') {
                    foreach (explode(',', $raw) as $part) {
                        $part = trim($part);
                        if ($part !== '') {
                            $accountChannelIds[] = (int)$part;
                        }
                    }
                }
                $username = isset($p['login_account']) ? (string)$p['login_account'] : '';
                $vmosid = isset($p['vmosid']) ? trim((string)$p['vmosid']) : '';
                if ($username === '' || $vmosid === '') {
                    continue;
                }
                foreach ($accountChannelIds as $cid) {
                    if (isset($channels[$cid])) {
                        $list[] = [
                            'username'      => $username,
                            'password'      => isset($p['login_password']) ? (string)$p['login_password'] : '',
                            'channelid'     => $cid,
                            'channel_type'  => $channels[$cid]['channel_type'],
                            'payparams_id'  => isset($p['id']) ? (int)$p['id'] : 0,
                            'appid'         => isset($p['appid']) ? trim((string)$p['appid']) : '',
                            'vmosid'        => $vmosid,
                            'vmosapp'       => isset($p['vmosapp']) ? trim((string)$p['vmosapp']) : '',
                        ];
                    }
                }
            }
            return $list;
        } catch (\Exception $e) {
            return [];
        }
    }

    /**
     * 按 channelid 升序排列的 APP 通道 ID 列表
     */
    protected function _sortedChannelIds() {
        $ids = [];
        foreach ($this->accounts as $a) {
            $cid = isset($a['channelid']) ? (int)$a['channelid'] : 0;
            if ($cid > 0) {
                $ids[$cid] = true;
            }
        }
        $list = array_keys($ids);
        sort($list, SORT_NUMERIC);
        return $list;
    }

    /**
     * 主入口：按 channelid 升序遍历通道；同通道内按 vmosid 分组再跑账号（减少云机切换），单进程顺序执行。
     */
    public function index() {
        try {
            if (php_sapi_name() !== 'cli') {
                $this->_output(['code' => -4, 'msg' => '请使用 CLI: php index.php Cli AutoVmosExchange index']);
                return;
            }
            $this->totalTokens = 0;
            $this->channelRunTokens = [];
            if (empty($this->accounts)) {
                $this->_output(['code' => -5, 'msg' => '无可用 APP 账户', 'error' => '检查 tw_paytype_config.is_web=2、tw_payparams_list.vmosid 与 channelid']);
                return;
            }
            $doc = $this->_readOpsDoc();
            $channelIds = $this->_sortedChannelIds();
            $summary = [];

            foreach ($channelIds as $channelid) {
                $batch = [];
                foreach ($this->accounts as $a) {
                    if ((int)$a['channelid'] === (int)$channelid) {
                        $batch[] = $a;
                    }
                }
                if (empty($batch)) {
                    continue;
                }
                $byPad = [];
                foreach ($batch as $acc) {
                    $pad = isset($acc['vmosid']) ? $acc['vmosid'] : '';
                    if (!isset($byPad[$pad])) {
                        $byPad[$pad] = [];
                    }
                    $byPad[$pad][] = $acc;
                }
                foreach ($byPad as $pad => $list) {
                    foreach ($list as $account) {
                        $r = $this->_runSingleAccountPipeline($account, $doc);
                        $summary[] = ['channelid' => $channelid, 'vmosid' => $pad, 'user' => $account['username'], 'result' => $r];
                    }
                }
            }

            $this->_output(['code' => 0, 'msg' => 'ok', 'data' => ['steps' => $summary]]);
        } catch (\Exception $e) {
            $this->_output(['code' => -3, 'msg' => 'exception', 'error' => $e->getMessage()]);
        } catch (\Throwable $e) {
            $this->_output(['code' => -3, 'msg' => 'error', 'error' => $e->getMessage()]);
        }
    }

    /**
     * 单账户一次「总控步骤」：与 Web index 类似，执行一步 viewReceipts/executePayment
     */
    protected function _runSingleAccountPipeline($account, &$doc) {
        $this->accounts = [$account];
        $step = $this->_getNextStep($doc);
        if ($step === null) {
            if ($this->totalTokens >= self::TOKEN_LIMIT) {
                return ['error' => 'token超限'];
            }
            $step = $this->_askAiForStep($doc);
            if ($step === null) {
                $step = ['action' => 'viewReceipts'];
            }
            $this->_appendStepToDoc($doc, $step);
        }
        $result = $this->_executeStep($step, $doc);
        if (isset($result['token_exceeded']) && $result['token_exceeded']) {
            return $result;
        }
        $this->_writeStepResultToDoc($doc, $step, $result);
        return $result;
    }

    protected function _getNextStep($doc) {
        if (empty($doc['pending_steps']) || !is_array($doc['pending_steps'])) {
            return null;
        }
        return $doc['pending_steps'][0];
    }

    protected function _askAiForStep($doc) {
        $accountsSummary = [];
        if (!empty($doc['accounts']) && is_array($doc['accounts'])) {
            foreach ($doc['accounts'] as $u => $a) {
                $accountsSummary[$u] = [
                    'checked_receipts' => isset($a['checked_receipts']) ? count($a['checked_receipts']) : 0,
                    'executed_payments' => isset($a['executed_payments']) ? count($a['executed_payments']) : 0,
                ];
            }
        }
        $prompt = "当前操作文档摘要：\n" . json_encode([
            'last_action' => isset($doc['last_action']) ? $doc['last_action'] : null,
            'last_result_summary' => isset($doc['last_result_summary']) ? $doc['last_result_summary'] : null,
            'accounts' => $accountsSummary,
        ], JSON_UNESCAPED_UNICODE) . "\n\n请只返回一个 JSON 对象，且仅包含以下之一，不要其他说明：\n"
            . "1) 查看收款：{\"action\":\"viewReceipts\"}\n"
            . "2) 执行付款：{\"action\":\"executePayment\",\"amount\":\"金额\",\"payee\":\"收款人\",\"memo\":\"备注（可选）\",\"username\":\"指定账户用户名（可选）\"}\n"
            . "3) 结束：{\"action\":\"done\"}";
        $messages = [['role' => 'user', 'content' => $prompt]];
        $res = $this->_callQwen($messages);
        if ($res === false || $this->totalTokens >= self::TOKEN_LIMIT) {
            return null;
        }
        $content = is_string($res['content']) ? trim($res['content']) : '';
        return $this->_parseStepJson($content);
    }

    protected function _callQwen($messages, $model = null) {
        if ($this->totalTokens >= self::TOKEN_LIMIT) {
            return false;
        }
        $key = trim((string)(C('AUTO_VMOS_EXCHANGE_QWEN_API_KEY') ?: C('DASHSCOPE_API_KEY') ?: ''));
        if ($key === '') {
            return false;
        }
        $model = $model ?: (C('AUTO_VMOS_EXCHANGE_QWEN_MODEL') ?: self::QWEN_MODEL_DEFAULT);
        $body = json_encode([
            'model' => $model,
            'messages' => $messages,
            'max_tokens' => 1024,
        ]);
        $ch = curl_init(self::QWEN_COMPAT_API);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'Authorization: Bearer ' . $key,
            ],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 60,
        ]);
        $response = curl_exec($ch);
        curl_close($ch);
        if ($response === false) {
            return false;
        }
        $data = json_decode($response, true);
        if (empty($data['choices'][0]['message']['content'])) {
            return false;
        }
        $usage = isset($data['usage']) ? $data['usage'] : [];
        $used = isset($usage['total_tokens']) ? (int)$usage['total_tokens'] : 0;
        if ($used <= 0 && isset($usage['prompt_tokens']) && isset($usage['completion_tokens'])) {
            $used = (int)$usage['prompt_tokens'] + (int)$usage['completion_tokens'];
        }
        $this->totalTokens += $used;
        return [
            'content' => $data['choices'][0]['message']['content'],
            'usage' => $usage,
        ];
    }

    protected function _callQwenReturnUsage($messages) {
        $key = trim((string)(C('AUTO_VMOS_EXCHANGE_QWEN_API_KEY') ?: C('DASHSCOPE_API_KEY') ?: ''));
        if ($key === '' || $this->_getChannelRunTokenUsed(0) >= self::TOKEN_LIMIT) {
            return false;
        }
        $model = C('AUTO_VMOS_EXCHANGE_QWEN_MODEL') ?: self::QWEN_MODEL_DEFAULT;
        $body = json_encode([
            'model' => $model,
            'messages' => $messages,
            'max_tokens' => 1024,
        ]);
        $ch = curl_init(self::QWEN_COMPAT_API);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'Authorization: Bearer ' . $key,
            ],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 60,
        ]);
        $response = curl_exec($ch);
        curl_close($ch);
        if ($response === false) {
            return false;
        }
        $data = json_decode($response, true);
        if (empty($data['choices'][0]['message']['content'])) {
            return false;
        }
        $usage = isset($data['usage']) ? $data['usage'] : [];
        return [
            'content' => $data['choices'][0]['message']['content'],
            'usage'   => $usage,
        ];
    }

    protected function _executeStep($step, $doc) {
        $action = isset($step['action']) ? $step['action'] : '';
        if ($action === 'done') {
            return ['done' => true];
        }
        if ($action === 'viewReceipts') {
            return $this->_runStepForAccount('viewReceipts', $step, $doc);
        }
        if ($action === 'executePayment') {
            $username = isset($step['username']) ? trim($step['username']) : '';
            if ($username !== '') {
                $account = $this->_getAccountByUsername($username);
                if ($account === null) {
                    return ['error' => 'Account not found: ' . $username];
                }
                $single = $this->_runVmScript('executePayment', $step, $doc, $account);
                $single['_account'] = $username;
                $cid = isset($account['channelid']) ? (int)$account['channelid'] : 0;
                $accountKey = $cid > 0 ? ($cid . '_' . $username) : $username;
                return ['by_account' => [$accountKey => $single], 'payment_id' => isset($single['payment_id']) ? $single['payment_id'] : null, 'amount' => isset($single['amount']) ? $single['amount'] : '', 'payee' => isset($single['payee']) ? $single['payee'] : '', '_account' => $username];
            }
            return $this->_runStepForAccount('executePayment', $step, $doc);
        }
        return ['error' => 'unknown action: ' . $action];
    }

    protected function _getAccountByUsername($username) {
        foreach ($this->accounts as $a) {
            if (isset($a['username']) && $a['username'] === $username) {
                return $a;
            }
        }
        return null;
    }

    protected function _runStepForAccount($action, $step, $doc) {
        $byAccount = [];
        $allCheckedReceipts = [];
        $summaryParts = [];
        $wantType = ($action === 'viewReceipts') ? 2 : (($action === 'executePayment') ? 1 : null);
        foreach ($this->accounts as $account) {
            $u = isset($account['username']) ? $account['username'] : '';
            if ($u === '') {
                continue;
            }
            if ($wantType !== null && isset($account['channel_type']) && (int)$account['channel_type'] !== $wantType) {
                continue;
            }
            $cid = isset($account['channelid']) ? (int)$account['channelid'] : 0;
            $accountKey = $cid > 0 ? ($cid . '_' . $u) : $u;
            $one = $this->_runVmScript($action, $step, $doc, $account);
            $one['_account'] = $u;

            if ($action === 'viewReceipts' && isset($one['receipts']) && is_array($one['receipts'])) {
                $submitResult = ExchangeCommon::submitReceiptsForAccount($this->runtimePath, $account, $one['receipts']);
                if (!empty($submitResult['submitted'])) {
                    $one['submitted_receipts'] = $submitResult['submitted'];
                }
                if (!empty($submitResult['skipped'])) {
                    $one['skipped_receipts'] = $submitResult['skipped'];
                }
                if (!empty($submitResult['summary'])) {
                    $one['summary'] = isset($one['summary']) && $one['summary'] !== ''
                        ? $one['summary'] . '; ' . $submitResult['summary']
                        : $submitResult['summary'];
                }
            }

            $byAccount[$accountKey] = $one;
            if (isset($one['checked_receipts']) && is_array($one['checked_receipts'])) {
                foreach ($one['checked_receipts'] as $r) {
                    $r['_account'] = $u;
                    $allCheckedReceipts[] = $r;
                }
            }
            if (isset($one['summary'])) {
                $summaryParts[] = $u . ':' . $one['summary'];
            }
        }
        $result = ['by_account' => $byAccount, 'checked_receipts' => $allCheckedReceipts, 'summary' => implode('; ', $summaryParts)];
        if ($action === 'executePayment' && count($byAccount) === 1) {
            $first = reset($byAccount);
            $result['payment_id'] = isset($first['payment_id']) ? $first['payment_id'] : null;
            $result['amount'] = isset($first['amount']) ? $first['amount'] : '';
            $result['payee'] = isset($first['payee']) ? $first['payee'] : '';
        }
        return $result;
    }

    /**
     * VMOS + Node(android_vm) 或占位：打开 ADB、执行文档驱动步骤
     */
    protected function _runVmScript($action, $step, $doc, $account) {
        $padCode = isset($account['vmosid']) ? trim($account['vmosid']) : '';
        if ($padCode === '') {
            return ['success' => false, 'error' => '缺少 vmosid'];
        }
        $orders = [];
        $payLog = null;

        $client = new VmosCloudClient();
        $open = $client->openOnlineAdb([$padCode], 1);
        if ($open === null || !isset($open['code']) || (int)$open['code'] !== 200) {
            @file_put_contents($this->runtimePath . '/vmos_open_adb.log', date('Y-m-d H:i:s') . ' ' . json_encode($open, JSON_UNESCAPED_UNICODE) . "\n", FILE_APPEND);
        }

        $baseUrl = C('AUTO_WEB_EXCHANGE_BASE_URL');
        $baseUrlForApi = $baseUrl ? rtrim($baseUrl, '/') . '/' : '';
        $username = isset($account['username']) ? $account['username'] : '';
        $channelid = isset($account['channelid']) ? (int)$account['channelid'] : 0;
        $accountKey = $channelid > 0 ? ($channelid . '_' . $username) : $username;

        $args = [
            'action' => $action,
            'username' => $username,
            'password' => isset($account['password']) ? $account['password'] : '',
            'pad_code' => $padCode,
            'vmosapp' => isset($account['vmosapp']) ? $account['vmosapp'] : '',
            'channelid' => $channelid,
            'channel_type' => isset($account['channel_type']) ? (int)$account['channel_type'] : 2,
            'account_key' => $accountKey,
        ];
        if ($baseUrlForApi !== '') {
            $args['doc_driven'] = true;
            $args['get_next_step_url'] = $baseUrlForApi . 'index.php?m=Cli&c=AutoVmosExchange&a=getNextStep';
            $args['report_result_url'] = $baseUrlForApi . 'index.php?m=Cli&c=AutoVmosExchange&a=reportStepResult';
        }
        $cacheSuffix = $channelid > 0 ? ($channelid . '_' . preg_replace('/[^a-zA-Z0-9_-]/', '_', $username)) : preg_replace('/[^a-zA-Z0-9_-]/', '_', $username);
        $args['cache_path'] = $this->runtimePath . '/vmos_receipt_checked_cache_' . $cacheSuffix . '.json';
        $args['otp_api_url'] = $baseUrl ? (rtrim($baseUrl, '/') . '/index.php?m=Cli&c=AutoWebExchange&a=getOtp') : '';
        $args['captcha_api_url'] = $baseUrl ? (rtrim($baseUrl, '/') . '/index.php?m=Cli&c=AutoWebExchange&a=recognizeCaptcha') : '';
        $args['otp_wait_timeout_sec'] = (int)(C('AUTO_WEB_EXCHANGE_OTP_TIMEOUT') ?: 180);
        $args['otp_poll_interval_sec'] = (int)(C('AUTO_WEB_EXCHANGE_OTP_POLL_INTERVAL') ?: 3);

        if ($action === 'viewReceipts' && $baseUrlForApi !== '') {
            $args['receipt_parse_url'] = $baseUrlForApi . 'index.php?m=Cli&c=AutoVmosExchange&a=getReceiptParseSpec';
            if ($channelid > 0) {
                $prefix = C('DB_PREFIX') ?: 'tw_';
                $cfg = M()->table($prefix . 'paytype_config')->where(['channelid' => $channelid])->field('receive_params')->find();
                if ($cfg && !empty($cfg['receive_params'])) {
                    $rp = json_decode($cfg['receive_params'], true);
                    if (is_array($rp)) {
                        $args['receive_params'] = $rp;
                    }
                }
            }
        }

        if ($action === 'executePayment') {
            $logFile = $this->runtimePath . '/vmos_payment_debug.log';
            $payLog = function ($msg) use ($logFile) {
                @file_put_contents($logFile, date('Y-m-d H:i:s') . ' ' . $msg . "\n", FILE_APPEND);
            };
            $prefix = C('DB_PREFIX') ?: 'tw_';
            $orderModel = D('ExchangeOrder');
            $payparamsId = isset($account['payparams_id']) ? (int)$account['payparams_id'] : 0;
            $bankcard = isset($account['appid']) ? trim((string)$account['appid']) : $username;

            $paymentParams = null;
            if ($channelid > 0) {
                $cfg = M()->table($prefix . 'paytype_config')->where(['channelid' => $channelid])->field('payment_params')->find();
                if ($cfg && !empty($cfg['payment_params'])) {
                    $pp = json_decode($cfg['payment_params'], true);
                    if (is_array($pp) && !empty($pp['amount_selector']) && !empty($pp['submit_selector'])
                        && (!empty($pp['payee_selector']) || !empty($pp['bankcard_selector']))) {
                        $paymentParams = $pp;
                    }
                }
            }
            if ($paymentParams === null) {
                return ['success' => false, 'error' => '通道未配置 payment_params 或必填选择器缺失'];
            }
            $orders = [];
            if ($payparamsId > 0 && $channelid > 0 && $orderModel) {
                $cutoff = time() - 86400;
                $orders = $orderModel
                    ->where([
                        'otype'         => 1,
                        'pay_channelid' => $channelid,
                        'status'        => 1,
                        'payparams_id'  => $payparamsId,
                        'task_status'   => 1,
                    ])
                    ->where('addtime >= ' . $cutoff)
                    ->order('addtime ASC')
                    ->field('orderid,truename,bank,bankcard,mum,pay_proof')
                    ->select();
                if (!is_array($orders)) {
                    $orders = [];
                }
            }
            if (empty($orders)) {
                return ['success' => true, 'order_results' => [], 'summary' => '无待付款订单'];
            }
            foreach ($orders as $o) {
                $orderModel->where(['orderid' => $o['orderid']])->save(['status' => 2, 'task_status' => 2]);
            }
            $args['orders'] = array_map(function ($o) {
                return [
                    'orderid'  => isset($o['orderid']) ? $o['orderid'] : '',
                    'truename' => isset($o['truename']) ? $o['truename'] : '',
                    'bank'     => isset($o['bank']) ? $o['bank'] : '',
                    'bankcard' => isset($o['bankcard']) ? $o['bankcard'] : '',
                    'mum'      => isset($o['mum']) ? $o['mum'] : '',
                ];
            }, $orders);
            $args['payment_params'] = $paymentParams;
            $args['bankcard'] = $bankcard;
            $args['amount'] = '';
            $args['payee'] = '';
            $args['memo'] = '';
        }

        if (!is_file($this->nodeScriptPath)) {
            $adb = $client->getAdbInfo($padCode, true);
            return [
                'success' => false,
                'error' => '未找到 VmosExecutor/run_android_vm.js，请添加 Node 执行器；已尝试 openOnlineAdb/getAdbInfo',
                'vmos_adb_response' => $adb,
                'args_preview' => array_merge($args, ['password' => '***']),
            ];
        }

        $cmd = 'node ' . escapeshellarg($this->nodeScriptPath) . ' ' . escapeshellarg(json_encode($args)) . ' 2>&1';
        $output = shell_exec($cmd);
        if ($output === null || $output === '') {
            return ['error' => 'run_android_vm.js 无输出'];
        }
        $out = trim($output);
        $lines = explode("\n", $out);
        $lastLine = '';
        foreach (array_reverse($lines) as $line) {
            $line = trim($line);
            if (strpos($line, '{') !== false) {
                $lastLine = $line;
                break;
            }
        }
        if ($lastLine === '') {
            $lastLine = $out;
        }
        $decoded = json_decode($lastLine, true);
        if (!is_array($decoded)) {
            return ['raw' => $lastLine];
        }
        if ($action === 'executePayment' && !empty($orders) && !empty($decoded['order_results']) && $payLog !== null) {
            ExchangeCommon::processPaymentOrderResults($this->runtimePath, $decoded['order_results'], $orders, $account, $payLog);
        }
        return $decoded;
    }

    public function getNextStep() {
        $raw = file_get_contents('php://input');
        $input = is_string($raw) ? json_decode($raw, true) : [];
        if (!is_array($input)) {
            $input = [];
        }
        $channelid = isset($input['channelid']) ? (int)$input['channelid'] : 0;
        $accountKey = isset($input['account_key']) ? trim((string)$input['account_key']) : '';
        $lastCompletedStepId = isset($input['last_completed_step_id']) ? trim((string)$input['last_completed_step_id']) : '';
        $pageContext = isset($input['pageContext']) && is_array($input['pageContext']) ? $input['pageContext'] : [];

        $account = $this->_getAccountByKey($accountKey);
        if ($channelid <= 0 && $account !== null) {
            $channelid = isset($account['channelid']) ? (int)$account['channelid'] : 0;
        }
        if ($channelid <= 0) {
            $this->_outputJson(['step' => null, 'done' => true, 'error' => 'missing channelid']);
            return;
        }

        $doc = $this->_readChannelDoc($channelid);
        $tokenUsed = $this->_getChannelRunTokenUsed($channelid);
        $channelType = $account && isset($account['channel_type']) ? (int)$account['channel_type'] : 2;

        if (empty($doc['steps'])) {
            if ($tokenUsed >= self::TOKEN_LIMIT) {
                $this->_outputJson(['step' => null, 'done' => true, 'error' => '该通道本次运行AI调用已达上限']);
                return;
            }
            $newSteps = $this->_askAiForChannelSteps($channelid, $doc, $channelType, $pageContext, null);
            if (!empty($newSteps)) {
                $doc['steps'] = $newSteps;
                $this->_writeChannelDoc($channelid, $doc);
                $step = $this->_pickStepAtPosition($doc['steps'], 0);
                if ($step !== null) {
                    $this->_outputJson(['step' => $step, 'done' => false]);
                    return;
                }
            }
            $this->_outputJson(['step' => null, 'done' => true, 'error' => 'AI 未返回可用步骤']);
            return;
        }

        $currentPosition = 0;
        if ($lastCompletedStepId !== '') {
            foreach ($doc['steps'] as $s) {
                if (isset($s['id']) && $s['id'] === $lastCompletedStepId && isset($s['position'])) {
                    $currentPosition = (int)$s['position'] + 1;
                    break;
                }
            }
        }

        $step = $this->_pickStepAtPosition($doc['steps'], $currentPosition);
        if ($step !== null) {
            $this->_outputJson(['step' => $step, 'done' => false]);
            return;
        }

        if ($tokenUsed >= self::TOKEN_LIMIT) {
            $this->_outputJson(['step' => null, 'done' => true, 'error' => '该通道本次运行AI调用已达上限']);
            return;
        }
        $newSteps = $this->_askAiForChannelSteps($channelid, $doc, $channelType, $pageContext, $currentPosition);
        if (!empty($newSteps)) {
            $doc['steps'] = array_merge($doc['steps'], $newSteps);
            $this->_writeChannelDoc($channelid, $doc);
            $this->_outputJson(['step' => $newSteps[0], 'done' => false]);
            return;
        }
        $this->_outputJson(['step' => null, 'done' => true]);
    }

    protected function _askAiForChannelSteps($channelid, &$doc, $channelType, $pageContext, $position) {
        $tokenUsed = $this->_getChannelRunTokenUsed($channelid);
        if ($tokenUsed >= self::TOKEN_LIMIT) {
            return [];
        }
        $url = isset($pageContext['url']) ? trim((string)$pageContext['url']) : '';
        $typeLabel = $channelType == 1 ? '付款' : '收款';
        if ($position === null) {
            $prompt = "当前是网银登录或操作页面。URL: {$url}\n通道类型：{$typeLabel}。\n"
                . "你的任务：规划从当前页面开始的一系列浏览器操作步骤，用于自动完成{$typeLabel}任务。\n\n"
                . "必须严格遵守以下规则：\n"
                . "1) 只返回一个 JSON 数组，不要任何解释文字、不要 markdown 代码块。例如：[ {...}, {...} ]。\n"
                . "2) 每个元素是一个步骤对象，格式：\n"
                . "   {\"type\":\"fill\"|\"click\"|\"viewReceipts\"|\"executePayment\",\n"
                . "    \"selector\":\"...\" (仅 fill/click 需要),\n"
                . "    \"value\":\"...\" (仅 fill 需要),\n"
                . "    \"position\":0 开始递增,\n"
                . "    \"optional\":true|false (是否为可选步骤，如首次登录才需要填写的姓名/身份证)}。\n"
                . "3) 登录页上的用户名/账号输入框步骤：type=fill，selector 指向用户名/账号输入框，value 必须严格等于 \"{{username}}\"，禁止写示例文字（如“你的网银用户名”）或真实账号。\n"
                . "4) 登录页上的密码输入框步骤：type=fill，selector 指向密码输入框，value 必须严格等于 \"{{password}}\"，禁止写示例文字或真实密码。\n"
                . "5) 若通道类型为收款（channel_type=2），最后一个步骤必须且只能是 type=\"viewReceipts\"，不要生成任何 executePayment 步骤。\n"
                . "6) 若通道类型为付款（channel_type=1），最后一个步骤必须且只能是 type=\"executePayment\"，不要生成任何 viewReceipts 步骤。\n"
                . "7) 除上述字段外不要返回多余字段，不要返回帐户、金额、姓名等具体业务数据，只使用 {{username}} 和 {{password}} 这两个占位符。\n\n"
                . "请根据以上规则返回步骤数组。";
        } else {
            $prompt = "当前页面 URL: {$url}\n通道类型：{$typeLabel}。当前执行到第 {$position} 步，该位置所有方案均失败。\n"
                . "只为 position={$position} 这一位置补充一个或多个可选操作步骤（可使用不同 selector）。\n"
                . "必须严格遵守以下规则：\n"
                . "1) 只返回一个 JSON 数组，不要任何解释文字。例如：[ {...}, {...} ]。\n"
                . "2) 每个元素格式：{\"type\":\"fill\"|\"click\",\"selector\":\"...\",\"value\":\"...\"(fill 时),\"optional\":true|false}。\n"
                . "3) 若是用户名输入框步骤，value 必须严格等于 \"{{username}}\"；若是密码输入框步骤，value 必须严格等于 \"{{password}}\"，禁止写示例文字或真实值。\n"
                . "4) 可选步骤（optional=true）表示：当页面不存在对应元素时可以安全跳过；必选步骤（optional=false）表示：正常流程中应始终存在该元素。\n"
                . "5) 此次仅补充 position={$position} 的 fill/click 步骤，不要返回 viewReceipts 或 executePayment。\n";
        }
        $messages = [['role' => 'user', 'content' => $prompt]];
        $res = $this->_callQwenReturnUsage($messages);
        if ($res === false) {
            return [];
        }
        $used = isset($res['usage']) ? (int)(isset($res['usage']['total_tokens']) ? $res['usage']['total_tokens'] : 0) : 0;
        if ($used <= 0 && isset($res['usage']['prompt_tokens']) && isset($res['usage']['completion_tokens'])) {
            $used = (int)$res['usage']['prompt_tokens'] + (int)$res['usage']['completion_tokens'];
        }
        $this->_addChannelRunTokenUsed($channelid, $used);
        $content = is_string($res['content']) ? trim($res['content']) : '';
        $steps = $this->_parseAiStepsJson($content, $position);
        return $this->_filterChannelSteps($steps, $channelType);
    }

    protected function _askAiForReceiptParseSpec($channelid, &$doc, $channelType, $pageContext) {
        $tokenUsed = $this->_getChannelRunTokenUsed($channelid);
        if ($tokenUsed >= self::TOKEN_LIMIT) {
            return null;
        }
        $url = isset($pageContext['url']) ? trim((string)$pageContext['url']) : '';
        $html = isset($pageContext['html']) ? trim((string)$pageContext['html']) : '';
        if ($html !== '' && strlen($html) > 20000) {
            $html = substr($html, 0, 20000);
        }
        $typeLabel = $channelType == 1 ? '付款' : '收款';
        $prompt = "以下是某网银{$typeLabel}通道的收款/交易列表页面 HTML 片段，请分析并返回用于解析收款记录的选择器规范。\n"
            . "当前页面 URL: {$url}\n\n"
            . "HTML 片段（可能被截断）：\n{$html}\n\n"
            . "请只返回一个 JSON 对象，不要任何解释，格式示例：\n"
            . "{\n"
            . "  \"row_selector\": \"table#txnTable tbody tr\",\n"
            . "  \"time_selector\": \"td:nth-child(1)\",\n"
            . "  \"amount_selector\": \"td:nth-child(3)\",\n"
            . "  \"row_id_selector\": \"td:nth-child(1)\",\n"
            . "  \"filters\": { \"amount_must_have_digit\": true }\n"
            . "}\n"
            . "含义：row_selector 选出每一行收款记录元素；time_selector/amount_selector 为相对行的选择器；\n"
            . "row_id_selector 用于从行中提取能标识该行的文本或属性（可选）；filters.amount_must_have_digit 为 true 时，金额文本不含数字的行应被忽略。";
        $messages = [['role' => 'user', 'content' => $prompt]];
        $res = $this->_callQwenReturnUsage($messages);
        if ($res === false) {
            return null;
        }
        $used = isset($res['usage']) ? (int)(isset($res['usage']['total_tokens']) ? $res['usage']['total_tokens'] : 0) : 0;
        if ($used <= 0 && isset($res['usage']['prompt_tokens']) && isset($res['usage']['completion_tokens'])) {
            $used = (int)$res['usage']['prompt_tokens'] + (int)$res['usage']['completion_tokens'];
        }
        $this->_addChannelRunTokenUsed($channelid, $used);
        $content = is_string($res['content']) ? trim($res['content']) : '';
        return $this->_parseReceiptParseSpecJson($content);
    }

    public function getReceiptParseSpec() {
        $raw = file_get_contents('php://input');
        $input = is_string($raw) ? json_decode($raw, true) : [];
        if (!is_array($input)) {
            $input = [];
        }
        $channelid = isset($input['channelid']) ? (int)$input['channelid'] : 0;
        $accountKey = isset($input['account_key']) ? trim((string)$input['account_key']) : '';
        $pageContext = isset($input['pageContext']) && is_array($input['pageContext']) ? $input['pageContext'] : [];
        $forceRefresh = !empty($input['force_refresh']);

        $account = $this->_getAccountByKey($accountKey);
        if ($channelid <= 0 && $account !== null) {
            $channelid = isset($account['channelid']) ? (int)$account['channelid'] : 0;
        }
        if ($channelid <= 0) {
            $this->_outputJson(['ok' => false, 'error' => 'missing channelid']);
            return;
        }

        $doc = $this->_readChannelDoc($channelid);
        if (!$forceRefresh && !empty($doc['receipt_parse']) && is_array($doc['receipt_parse']) && !empty($doc['receipt_parse']['row_selector']) && !empty($doc['receipt_parse']['amount_selector'])) {
            $this->_outputJson(['ok' => true, 'spec' => $doc['receipt_parse']]);
            return;
        }

        $channelType = $account && isset($account['channel_type']) ? (int)$account['channel_type'] : 2;
        $spec = $this->_askAiForReceiptParseSpec($channelid, $doc, $channelType, $pageContext);
        if ($spec === null) {
            $this->_outputJson(['ok' => false, 'error' => 'AI 未返回解析规范']);
            return;
        }
        $doc['receipt_parse'] = $spec;
        $this->_writeChannelDoc($channelid, $doc);
        $this->_outputJson(['ok' => true, 'spec' => $spec]);
    }

    public function reportStepResult() {
        $raw = file_get_contents('php://input');
        $input = is_string($raw) ? json_decode($raw, true) : [];
        if (!is_array($input)) {
            $input = [];
        }
        $channelid = isset($input['channelid']) ? (int)$input['channelid'] : 0;
        $step = isset($input['step']) && is_array($input['step']) ? $input['step'] : null;
        $failureType = isset($input['failure_type']) ? trim((string)$input['failure_type']) : '';

        if ($step === null || !isset($step['id'])) {
            $this->_outputJson(['ok' => false, 'error' => 'missing step or step.id']);
            return;
        }
        if ($channelid <= 0) {
            $this->_outputJson(['ok' => false, 'error' => 'missing channelid']);
            return;
        }

        $stepId = isset($step['id']) ? $step['id'] : '';
        @file_put_contents(
            $this->runtimePath . '/vmos_report_step_result.log',
            date('Y-m-d H:i:s') . ' channelid=' . $channelid . ' step_id=' . $stepId . ' failure_type=' . $failureType . "\n",
            FILE_APPEND
        );

        $doc = $this->_readChannelDoc($channelid);
        $foundIndex = null;
        foreach ($doc['steps'] as $i => $s) {
            if (isset($s['id']) && $s['id'] === $stepId) {
                $foundIndex = $i;
                break;
            }
        }
        if ($foundIndex === null) {
            $this->_outputJson(['ok' => true]);
            return;
        }

        if ($failureType === 'account_failure') {
            $this->_outputJson(['ok' => true]);
            return;
        }

        $weight = isset($doc['steps'][$foundIndex]['weight']) ? (int)$doc['steps'][$foundIndex]['weight'] : self::STEP_WEIGHT_INITIAL;
        if ($failureType === 'success') {
            $weight = min(self::STEP_WEIGHT_MAX, $weight + self::STEP_WEIGHT_SUCCESS_DELTA);
            $doc['steps'][$foundIndex]['weight'] = $weight;
        } else {
            $weight += self::STEP_WEIGHT_FAIL_DELTA;
            if ($weight <= 0) {
                array_splice($doc['steps'], $foundIndex, 1);
            } else {
                $doc['steps'][$foundIndex]['weight'] = $weight;
            }
        }
        $this->_writeChannelDoc($channelid, $doc);
        $this->_outputJson(['ok' => true]);
    }

    protected function _getAccountByKey($accountKey) {
        if ($accountKey === '') {
            return !empty($this->accounts) ? $this->accounts[0] : null;
        }
        foreach ($this->accounts as $a) {
            $u = isset($a['username']) ? $a['username'] : '';
            $cid = isset($a['channelid']) ? (int)$a['channelid'] : 0;
            $key = $cid > 0 ? ($cid . '_' . $u) : $u;
            if ($key === $accountKey) {
                return $a;
            }
        }
        return !empty($this->accounts) ? $this->accounts[0] : null;
    }
}
