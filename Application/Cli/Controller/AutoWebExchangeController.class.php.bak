<?php
namespace Cli\Controller;

use Think\Controller;
use Think\Log;
/**
 * 自动网银交换：任务编排、AI 调用、操作文档、调 Node 执行浏览器操作
 * ThinkPHP 3.2 Cli Controller
 *
 * 多账户与登录 URL：从数据库读取「网站操作」通道及账号。
 *   tw_paytype_config：渠道表，channelid, is_web(1=网站 2=APP), channel_type(1=付款 2=收款), website_url, proxy_country
 *   tw_payparams_list：账号表，channelid（可逗号分隔如 301,3001 表示账号归属多渠道）, login_account, login_password
 *   仅取 is_web=1 的渠道；账号的 channelid 若为「301,3001」则展开为多条，每条对应一个渠道的 website_url。
 * 收款解析：交给 AI 分析列表结构（行/时间/金额等），不再在代码中硬编码列。
 * 验证码：表 tw_exchange_auto_payment_code（account 唯一），getOtp/setOtp 读写，Node 轮询 getOtp；图片验证码 PHP recognizeCaptcha（AI 兜底）。
 *   AUTO_WEB_EXCHANGE_BASE_URL => 'http://你的域名或IP'  // Node 调 getOtp/recognizeCaptcha 的根 URL
 *   AUTO_WEB_EXCHANGE_OTP_TIMEOUT => 180   // 等待验证码超时（秒），默认 3 分钟
 *   AUTO_WEB_EXCHANGE_OTP_POLL_INTERVAL => 3   // 轮询间隔（秒）
 *   AUTO_WEB_EXCHANGE_USE_PROXY => false   // 【配置项】是否使用代理。false 或不设：不传代理（兼容 Browserbase 免费版）；true：使用代理，代理参数从数据库渠道表获取
 *   // 当 USE_PROXY 为 true 时，代理国家/城市/州优先从数据库 tw_paytype_config.proxy_country 等获取，缺省时用下面配置补全：
 *   AUTO_WEB_EXCHANGE_PROXY_COUNTRY => 'TH'   // 渠道未配置时代理国家码
 *   AUTO_WEB_EXCHANGE_PROXY_CITY => 'BANGKOK'   // 可选，代理城市
 *   AUTO_WEB_EXCHANGE_PROXY_STATE => 'NY'   // 可选，美国时填州
 *   AUTO_WEB_EXCHANGE_PROXY => true   // 不指定国家时使用内置代理（默认美国）
 *   文档驱动（打开页面后读文档→无下一步则 AI/默认步骤→执行并写回）：
 *   AUTO_WEB_EXCHANGE_LOGIN_BROWSER_STEPS => [ ['type'=>'fill','selector'=>'#USERID','value'=>'{{username}}'], ['type'=>'fill','selector'=>'#LOGINPWD','value'=>'{{password}}'], ['type'=>'click','selector'=>'...'] ]  // 可选，不配则用内置建行示例
 *  Browserbase：由 PHP 配置传入 Node，不依赖 .env
 *   BROWSERBASE_API_KEY => '你的 API Key'
 *   BROWSERBASE_PROJECT_ID => '你的 Project ID'
 * 步骤共用（pending_steps），状态按账户记录在 doc.accounts[username]。
 */
class AutoWebExchangeController extends \Think\Controller {

    /** 本次运行累计 token */
    protected $totalTokens = 0;
    /** 每通道 AI token 上限（601 达限不影响 6001） */
    const TOKEN_LIMIT = 8000;
    /** 步骤权重：初始 8，上限 10；成功 +1，步骤失败 -2，≤0 删除 */
    const STEP_WEIGHT_INITIAL = 8;
    const STEP_WEIGHT_MAX = 10;
    const STEP_WEIGHT_SUCCESS_DELTA = 1;
    const STEP_WEIGHT_FAIL_DELTA = -2;

    /** DeepSeek API Key */
    const DEEPSEEK_KEY = 'sk-73a3298a11c64ecca30eac0add899c89';
    /** DeepSeek API URL */
    const DEEPSEEK_API = 'https://api.deepseek.com/v1/chat/completions';
    /** 操作文档路径（可改为配置） */
    protected $opsDocPath;
    /** 通道操作文档目录：Public/WebAutoScriptDoc，每通道一个 JSON（channelid.json） */
    protected $webAutoScriptDocDir;
    /** Node 脚本路径（PwBrowse 目录下的入口） */
    protected $nodeScriptPath;
    /** Runtime 目录（用于按账户的缓存路径） */
    protected $runtimePath;
    /**
     * 多账户列表： [ ['username'=>'xx','password'=>'xx','channelid'=>301,'website_url'=>'https://...'], ... ]
     * 优先从 tw_paytype_config + tw_payparams_list 读取（is_web=1），否则从配置 C('AUTO_WEB_EXCHANGE_ACCOUNTS')
     */
    protected $accounts = [];
    /** 本次运行中各通道已消耗的 AI token，用于按次限制，不再持久化到文档 */
    protected $channelRunTokens = [];

    public function __construct() {
        parent::__construct();
        $root = defined('ROOT_PATH') ? ROOT_PATH : dirname(dirname(dirname(dirname(__FILE__))));
        $this->opsDocPath = $root . '/Runtime/auto_web_exchange_ops.json';
        $this->webAutoScriptDocDir = $root . '/Public/WebAutoScriptDoc';
        $this->nodeScriptPath = $root . '/PwBrowse/run.js';
        $this->runtimePath = $root . '/Runtime';
        $this->accounts = $this->_getAccounts();
    }

    /**
     * 获取账户列表：仅「网站操作」渠道（tw_paytype_config.is_web=1）
     * tw_payparams_list.channelid 可为逗号分隔（如 301,3001），一账号归属多渠道时展开为多条
     * 返回 [ ['username'=>login_account,'password'=>login_password,'channelid'=>x,'website_url'=>url], ... ]
     */
    /**
     * 仅从数据库读取账户，无默认值。无数据或异常时返回空数组。
     */
    protected function _getAccounts() {
        $prefix = defined('C') ? C('DB_PREFIX') : 'tw_';
        $configTable = $prefix . 'paytype_config';
        $paramsTable = $prefix . 'payparams_list';
        try {
            $channelRows = M()->table($configTable)->where(['is_web' => 1])->field('channelid,website_url,proxy_country,channel_type')->select();
            if (!is_array($channelRows) || empty($channelRows)) {
                return [];
            }
            $channels = [];
            foreach ($channelRows as $c) {
                $cid = isset($c['channelid']) ? (int)$c['channelid'] : 0;
                $url = isset($c['website_url']) ? trim((string)$c['website_url']) : '';
                if ($cid > 0 && $url !== '') {
                    $proxyCountry = isset($c['proxy_country']) ? trim((string)$c['proxy_country']) : '';
                    $channelType = isset($c['channel_type']) ? (int)$c['channel_type'] : 2;
                    $channels[$cid] = ['channelid' => $cid, 'website_url' => $url, 'proxy_country' => $proxyCountry, 'channel_type' => $channelType];
                }
            }
            if (empty($channels)) {
                return [];
            }
            $accountRows = M()->table($paramsTable)->field('id,channelid,login_account,login_password,appid')->select();
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
                $password = isset($p['login_password']) ? (string)$p['login_password'] : '';
                if ($username === '') {
                    continue;
                }
                foreach ($accountChannelIds as $cid) {
                    if (isset($channels[$cid])) {
                        $entry = [
                            'username'      => $username,
                            'password'      => $password,
                            'channelid'     => $cid,
                            'channel_type'  => $channels[$cid]['channel_type'],
                            'website_url'   => $channels[$cid]['website_url'],
                            'payparams_id'  => isset($p['id']) ? (int)$p['id'] : 0,
                            'appid'         => isset($p['appid']) ? trim((string)$p['appid']) : '',
                        ];
                        if ($channels[$cid]['proxy_country'] !== '') {
                            $entry['proxy_country'] = $channels[$cid]['proxy_country'];
                        }
                        $list[] = $entry;
                    }
                }
            }
            return $list;
        } catch (\Exception $e) {
            return [];
        }
    }

    /**
     * 主入口：执行一次任务（查收款或付款等）
     * 用法：php index.php Cli AutoWebExchange index  或  php index.php Cli/AutoWebExchange/index
     */
    public function index() {
        try {
            if (php_sapi_name() !== 'cli') {
                $this->_output(['code' => -4, 'msg' => '请使用 CLI 运行: php index.php Cli/AutoWebExchange/index']);
                return;
            }
            $this->totalTokens = 0;
            if (empty($this->accounts)) {
                $this->_output(['code' => -5, 'msg' => '无可用账户', 'error' => '请检查 tw_paytype_config（is_web=1）与 tw_payparams_list 中是否有渠道与账号，且 website_url 非空']);
                return;
            }
            $doc = $this->_readOpsDoc();

            // 若通过 CLI / 请求参数显式指定了 action（viewReceipts 或 executePayment），则不再由 AI 决定总控步骤，直接按该 action 执行一次
            $reqAction = trim((string)I('request.action', '', 'strip_tags'));
            if ($reqAction !== '') {
                $action = strtolower($reqAction);
                if ($action === 'viewreceipts') {
                    $step = ['action' => 'viewReceipts'];
                } elseif ($action === 'executepayment') {
                    $step = [
                        'action' => 'executePayment',
                        'amount' => I('request.amount', '', 'strip_tags'),
                        'payee'  => I('request.payee', '', 'strip_tags'),
                        'memo'   => I('request.memo', '', 'strip_tags'),
                        'username' => I('request.username', '', 'strip_tags'),
                    ];
                } else {
                    $this->_output(['code' => -6, 'msg' => 'unknown action', 'error' => '不支持的 action: ' . $reqAction]);
                    return;
                }

                $result = $this->_executeStep($step, $doc);
                if (isset($result['token_exceeded']) && $result['token_exceeded']) {
                    $this->_output(['code' => -1, 'msg' => 'token超限']);
                    return;
                }
                $this->_writeStepResultToDoc($doc, $step, $result);
                $this->_output(['code' => 0, 'msg' => 'ok', 'data' => $result]);
                return;
            }

            // 1. 若文档中已有明确下一步，则用文档步骤；否则调 AI 生成
            $step = $this->_getNextStep($doc);
            if ($step === null) {
                if ($this->totalTokens >= self::TOKEN_LIMIT) {
                    $this->_output(['code' => -1, 'msg' => 'token超限']);
                    return;
                }
                $step = $this->_askAiForStep($doc);
                if ($step === null) {
                    // AI 无有效步骤时默认先查收款，便于单次跑通
                    $step = ['action' => 'viewReceipts'];
                }
                $this->_appendStepToDoc($doc, $step);
            }

            // 2. 执行步骤（浏览器任务交给 Node）
            $result = $this->_executeStep($step, $doc);
            if (isset($result['token_exceeded']) && $result['token_exceeded']) {
                $this->_output(['code' => -1, 'msg' => 'token超限']);
                return;
            }

            // 3. 将执行结果写回文档
            $this->_writeStepResultToDoc($doc, $step, $result);

            $this->_output(['code' => 0, 'msg' => 'ok', 'data' => $result]);
        } catch (\Exception $e) {
            $this->_output(['code' => -3, 'msg' => 'exception', 'error' => $e->getMessage(), 'file' => $e->getFile(), 'line' => $e->getLine()]);
        } catch (\Throwable $e) {
            $this->_output(['code' => -3, 'msg' => 'error', 'error' => $e->getMessage(), 'file' => $e->getFile(), 'line' => $e->getLine()]);
        }
    }

    /**
     * 从文档中读取“下一步”步骤（若存在且未执行完）
     */
    protected function _getNextStep($doc) {
        if (empty($doc['pending_steps']) || !is_array($doc['pending_steps'])) {
            return null;
        }
        return $doc['pending_steps'][0];
    }

    /**
     * 调 AI 获取下一步操作建议（返回 step 结构）
     */
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
            . "2) 执行付款：{\"action\":\"executePayment\",\"amount\":\"金额\",\"payee\":\"收款人\",\"memo\":\"备注（可选）\",\"username\":\"指定账户用户名（可选，不填则对所有账户执行）\"}\n"
            . "3) 结束：{\"action\":\"done\"}";
        $messages = [
            ['role' => 'user', 'content' => $prompt],
        ];
        $res = $this->_callDeepSeek($messages);
        if ($res === false || $this->totalTokens >= self::TOKEN_LIMIT) {
            return null;
        }
        $content = is_string($res['content']) ? trim($res['content']) : '';
        $step = $this->_parseStepJson($content);
        return $step;
    }

    /**
     * 从 AI 返回文本中解析出 step（支持纯 JSON、markdown 代码块、多字段对象）
     */
    protected function _parseStepJson($content) {
        if ($content === '') {
            return null;
        }
        $content = preg_replace('/^```\w*\s*|\s*```\s*$/s', '', $content);
        $content = trim($content);
        $step = json_decode($content, true);
        if (is_array($step) && !empty($step['action'])) {
            return $step;
        }
        $start = strpos($content, '{"action"');
        if ($start === false) {
            $start = strpos($content, '{');
        }
        if ($start !== false) {
            $depth = 0;
            $len = strlen($content);
            for ($i = $start; $i < $len; $i++) {
                if ($content[$i] === '{') {
                    $depth++;
                } elseif ($content[$i] === '}') {
                    $depth--;
                    if ($depth === 0) {
                        $step = json_decode(substr($content, $start, $i - $start + 1), true);
                        if (is_array($step) && !empty($step['action'])) {
                            return $step;
                        }
                        break;
                    }
                }
            }
        }
        return null;
    }

    /**
     * 调用 DeepSeek，累加 token，超限则不再请求
     */
    protected function _callDeepSeek($messages, $model = 'deepseek-chat') {
        if ($this->totalTokens >= self::TOKEN_LIMIT) {
            return false;
        }
        $body = json_encode([
            'model' => $model,
            'messages' => $messages,
            'max_tokens' => 1024,
        ]);
        $ch = curl_init(self::DEEPSEEK_API);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'Authorization: Bearer ' . self::DEEPSEEK_KEY,
            ],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 60,
        ]);
        $response = curl_exec($ch);
        $err = curl_error($ch);
        curl_close($ch);
        if ($err || $response === false) {
            return false;
        }
        $data = json_decode($response, true);
        if (empty($data['choices'][0]['message']['content'])) {
            return false;
        }
        $usage = isset($data['usage']) ? $data['usage'] : [];
        $used = isset($usage['total_tokens']) ? (int)$usage['total_tokens'] : (isset($usage['prompt_tokens']) && isset($usage['completion_tokens']) ? (int)$usage['prompt_tokens'] + (int)$usage['completion_tokens'] : 0);
        $this->totalTokens += $used;
        return [
            'content' => $data['choices'][0]['message']['content'],
            'usage' => $usage,
        ];
    }

    /**
     * 执行一步：浏览器类任务交给 Node
     * viewReceipts：对所有账户各执行一次 Node，合并结果
     * executePayment：若 step 含 username 则对该账户执行，否则对所有账户执行
     */
    protected function _executeStep($step, $doc) {
        $action = isset($step['action']) ? $step['action'] : '';
        if ($action === 'done') {
            return ['done' => true];
        }
        if ($action === 'viewReceipts') {
            return $this->_runStepForAllAccounts('viewReceipts', $step, $doc);
        }
        if ($action === 'executePayment') {
            $username = isset($step['username']) ? trim($step['username']) : '';
            if ($username !== '') {
                $account = $this->_getAccountByUsername($username);
                if ($account === null) {
                    return ['error' => 'Account not found: ' . $username];
                }
                $single = $this->_runNodeScript('executePayment', $step, $doc, $account);
                $single['_account'] = $username;
                $cid = isset($account['channelid']) ? (int)$account['channelid'] : 0;
                $accountKey = $cid > 0 ? ($cid . '_' . $username) : $username;
                return ['by_account' => [$accountKey => $single], 'payment_id' => isset($single['payment_id']) ? $single['payment_id'] : null, 'amount' => isset($single['amount']) ? $single['amount'] : '', 'payee' => isset($single['payee']) ? $single['payee'] : '', '_account' => $username];
            }
            return $this->_runStepForAllAccounts('executePayment', $step, $doc);
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

    /**
     * 对每个账户执行同一步骤，合并结果。viewReceipts 只跑收款通道(2)，executePayment 只跑付款通道(1)
     */
    protected function _runStepForAllAccounts($action, $step, $doc) {
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
            $one = $this->_runNodeScript($action, $step, $doc, $account);
            $one['_account'] = $u;

            // 若是收款查询(viewReceipts)，在此基础上按规则增量提交至 /Pay/PayExchange/orderQueren，并更新账号任务状态
            if ($action === 'viewReceipts' && isset($one['receipts']) && is_array($one['receipts'])) {
                $submitResult = $this->_submitReceiptsForAccount($account, $one['receipts']);
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
     * 将 Node 返回的收款记录按账户增量提交到 /Pay/PayExchange/orderQueren，并更新 tw_payparams_list 任务状态/时间
     * @param array $account 来自 _getAccounts 的账户信息（含 username/channelid 等）
     * @param array $receipts Node 返回的 receipts 数组，每项至少含 id/received_at/amount，可选 returnOrderID
     * @return array { submitted: [...], skipped: [...], summary: string }
     */
    protected function _submitReceiptsForAccount($account, $receipts) {
        $result = [
            'submitted' => [],
            'skipped'   => [],
            'summary'   => '',
        ];
        $username = isset($account['username']) ? $account['username'] : '';
        if ($username === '' || empty($receipts)) {
            return $result;
        }

        $prefix = C('DB_PREFIX') ?: 'tw_';
        $configTable = $prefix . 'paytype_config';
        $paramsTable = $prefix . 'payparams_list';
        $currTable   = $prefix . 'currencys';
        $peAdminTable = $prefix . 'pe_admin';

        $channelid = isset($account['channelid']) ? (int)$account['channelid'] : 0;

        // 读取通道配置（包括 receive_params / currencyid 等）
        $cfgRow = null;
        if ($channelid > 0) {
            $cfgRow = M()->table($configTable)->where(['channelid' => $channelid])->field('currencyid')->find();
        }

        // 读取账号配置（获取 userid/appid/task_status/task_success_time）
        $paramRow = M()->table($paramsTable)->where(['login_account' => $username])->field('userid,appid,task_status,task_success_time')->find();
        $taskStatus = $paramRow && isset($paramRow['task_status']) ? (int)$paramRow['task_status'] : 0;
        $taskTime   = $paramRow && isset($paramRow['task_success_time']) ? (int)$paramRow['task_success_time'] : 0;

        // 起始水位：上次成功时间，否则最近 6 小时
        $now = time();
        if ($taskStatus === 2 && $taskTime > 0) {
            $startTs = $taskTime;
        } else {
            $startTs = $now - 21600;
        }

        // 解析 currency
        $currency = 'MMK';
        if ($cfgRow && isset($cfgRow['currencyid']) && (int)$cfgRow['currencyid'] > 0) {
            $cid = (int)$cfgRow['currencyid'];
            $cRow = M()->table($currTable)->where(['id' => $cid])->field('currency')->find();
            if ($cRow && !empty($cRow['currency'])) {
                $currency = $cRow['currency'];
            }
        }

        // 其他固定参数：opUserID 用 tw_pe_admin.id（tw_pe_admin.userid = tw_payparams_list.userid）
        $opUserId = '';
        if ($paramRow && isset($paramRow['userid'])) {
            $peAdminRow = M()->table($peAdminTable)->where(['userid' => $paramRow['userid']])->field('id')->find();
            $opUserId = ($peAdminRow && isset($peAdminRow['id'])) ? $peAdminRow['id'] : '';
        }
        $bankcard   = $paramRow && isset($paramRow['appid']) ? $paramRow['appid'] : '';
        $paypassword = '123456';

        $baseUrl = C('AUTO_WEB_EXCHANGE_BASE_URL');
        if (!$baseUrl) {
            return $result;
        }
        $orderUrl = rtrim($baseUrl, '/') . '/Pay/PayExchange/orderQueren';
        $md5Key   = 'i8cuejg93k03kkuakdHDIhuyidOcUamp';

        $allOk = true;
        $maxTs = $taskTime;

        foreach ($receipts as $r) {
            $dateStr = isset($r['received_at']) ? trim($r['received_at']) : '';
            $amount  = isset($r['amount']) ? trim($r['amount']) : '';
            $orderId = isset($r['returnOrderID']) ? trim($r['returnOrderID']) : (isset($r['id']) ? trim($r['id']) : '');

            if ($dateStr === '' || $amount === '' || $orderId === '') {
                $result['skipped'][] = $r;
                continue;
            }

            // 将 YYYY-MM-DD HH:ii:ss 转为时间戳
            $ts = strtotime($dateStr);
            if ($ts === false) {
                $result['skipped'][] = $r;
                continue;
            }

            // 增量过滤：仅提交大于上次成功时间的记录
            if ($ts <= $startTs) {
                $result['skipped'][] = $r;
                continue;
            }

            $payload = [
                'returnOrderID' => $orderId,
                'bankcard'      => $bankcard,
                'amount'        => $amount,
                'opUserID'      => $opUserId,
                'paypassword'   => $paypassword,
                'currency'      => $currency,
                'date'          => $dateStr,
                'noticestr'     => md5(uniqid('', true)),
            ];
            $payload['sign'] = $this->_createSign($md5Key, $payload);

            // 提交到后端接口
            $ch = curl_init($orderUrl);
            curl_setopt_array($ch, [
                CURLOPT_POST           => true,
                CURLOPT_POSTFIELDS     => http_build_query($payload),
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_TIMEOUT        => 30,
            ]);
            $resp = curl_exec($ch);
            $err  = curl_error($ch);
            $httpCode = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
            curl_close($ch);

            // 请求/响应日志，便于排查
            $logLine = date('Y-m-d H:i:s') . ' account=' . $username . ' orderQueren request url=' . $orderUrl . ' payload=' . json_encode($payload, JSON_UNESCAPED_UNICODE) . ' response_http_code=' . $httpCode . ' response_body=' . (is_string($resp) ? $resp : '') . ' curl_error=' . $err . "\n";
            @file_put_contents($this->runtimePath . '/order_queren_submit.log', $logLine, FILE_APPEND);

            $ok = false;
            if (!$err && $resp !== false) {
                $respArr = json_decode($resp, true);
                if (is_array($respArr) && isset($respArr['status']) && strtolower($respArr['status']) === 'success') {
                    $ok = true;
                }
            }

            if ($ok) {
                $result['submitted'][] = $r;
                if ($ts > $maxTs) {
                    $maxTs = $ts;
                }
            } else {
                $allOk = false;
                $result['skipped'][] = $r;
            }
        }

        // 根据整体提交结果更新账号任务状态
        if (!empty($paramRow)) {
            if ($allOk && $maxTs > 0) {
                M()->table($paramsTable)->where(['login_account' => $username])->save([
                    'task_status'       => 2,
                    'task_success_time' => $maxTs,
                ]);
                $result['summary'] = 'submitted ' . count($result['submitted']) . ' receipt(s)';
            } else {
                M()->table($paramsTable)->where(['login_account' => $username])->save([
                    'task_status' => 1,
                ]);
                if (empty($result['submitted'])) {
                    $result['summary'] = 'no receipt submitted';
                } else {
                    $result['summary'] = 'partially submitted ' . count($result['submitted']) . ' receipt(s)';
                }
            }
        }

        return $result;
    }

    /**
     * 创建签名（按照业务方提供的规则）
     * @param string $md5key
     * @param array  $list
     * @return string
     */
    protected function _createSign($md5key, $list) {
        if (!is_array($list)) {
            return '';
        }
        ksort($list);
        reset($list);
        $md5str = '';
        foreach ($list as $key => $val) {
            if (($val !== '' && $val !== []) && $key !== 'sign') {
                if (is_array($val)) {
                    $md5str .= $key . '=' . json_encode($val) . '&';
                } else {
                    $md5str .= $key . '=' . $val . '&';
                }
            }
        }
        $md5str .= 'key=' . $md5key;
        return strtoupper(md5($md5str));
    }

    /**
     * 调用 PwBrowse 下的 Node 脚本执行浏览器操作（单账户）
     * @param array $account ['username'=>'xx','password'=>'xx']
     */
    protected function _runNodeScript($action, $step, $doc, $account) {
        $username = isset($account['username']) ? $account['username'] : '';
        $password = isset($account['password']) ? $account['password'] : '';
        $channelid = isset($account['channelid']) ? (int)$account['channelid'] : 0;
        $cacheSuffix = $channelid > 0 ? ($channelid . '_' . preg_replace('/[^a-zA-Z0-9_-]/', '_', $username)) : preg_replace('/[^a-zA-Z0-9_-]/', '_', $username);
        $cachePath = $this->runtimePath . '/receipt_checked_cache_' . $cacheSuffix . '.json';
        $baseUrl = C('AUTO_WEB_EXCHANGE_BASE_URL');
        $getOtpUrl = $baseUrl ? (rtrim($baseUrl, '/') . '/index.php?m=Cli&c=AutoWebExchange&a=getOtp') : '';
        $captchaUrl = $baseUrl ? (rtrim($baseUrl, '/') . '/index.php?m=Cli&c=AutoWebExchange&a=recognizeCaptcha') : '';
        $websiteUrl = isset($account['website_url']) ? trim((string)$account['website_url']) : '';
        $baseUrlForApi = $baseUrl ? rtrim($baseUrl, '/') . '/' : '';
        $args = [
            'action' => $action,
            'username' => $username,
            'password' => $password,
            'cache_path' => $cachePath,
            'website_url' => $websiteUrl,
            'otp_api_url' => $getOtpUrl,
            'captcha_api_url' => $captchaUrl,
            'otp_wait_timeout_sec' => (int)(C('AUTO_WEB_EXCHANGE_OTP_TIMEOUT') ?: 180),
            'otp_poll_interval_sec' => (int)(C('AUTO_WEB_EXCHANGE_OTP_POLL_INTERVAL') ?: 3),
        ];
        if ($baseUrlForApi !== '') {
            $args['doc_driven'] = true;
            $args['get_next_step_url'] = $baseUrlForApi . 'index.php?m=Cli&c=AutoWebExchange&a=getNextStep';
            $args['report_result_url'] = $baseUrlForApi . 'index.php?m=Cli&c=AutoWebExchange&a=reportStepResult';
            $args['account_key'] = $channelid > 0 ? ($channelid . '_' . $username) : $username;
            $args['channelid'] = $channelid;
            $args['channel_type'] = isset($account['channel_type']) ? (int)$account['channel_type'] : 2;
        }
        $bbKey = C('BROWSERBASE_API_KEY');
        $bbProject = C('BROWSERBASE_PROJECT_ID');
        if ($bbKey !== null && $bbKey !== '' && $bbKey !== false) {
            $args['browserbase_api_key'] = $bbKey;
        }
        if ($bbProject !== null && $bbProject !== '' && $bbProject !== false) {
            $args['browserbase_project_id'] = $bbProject;
        }
        // 是否使用代理：仅由配置项 AUTO_WEB_EXCHANGE_USE_PROXY 控制；代理参数从数据库渠道 account 获取，缺省时用配置补全
        $useProxy = C('AUTO_WEB_EXCHANGE_USE_PROXY');
        if ($useProxy) {
            $proxyCountry = isset($account['proxy_country']) ? trim((string)$account['proxy_country']) : '';
            if ($proxyCountry === '') {
                $proxyCountry = C('AUTO_WEB_EXCHANGE_PROXY_COUNTRY') ?: '';
            }
            $proxyCity = C('AUTO_WEB_EXCHANGE_PROXY_CITY');
            $proxyState = C('AUTO_WEB_EXCHANGE_PROXY_STATE');
            if ($proxyCountry !== '') {
                $args['proxy_country'] = $proxyCountry;
                if ($proxyCity !== null && $proxyCity !== '' && $proxyCity !== false) {
                    $args['proxy_city'] = $proxyCity;
                }
                if ($proxyState !== null && $proxyState !== '' && $proxyState !== false) {
                    $args['proxy_state'] = $proxyState;
                }
            } elseif (C('AUTO_WEB_EXCHANGE_PROXY')) {
                $args['use_proxy'] = true;
            }
        }
        if ($action === 'viewReceipts') {
            if ($baseUrlForApi !== '') {
                $args['receipt_parse_url'] = $baseUrlForApi . 'index.php?m=Cli&c=AutoWebExchange&a=getReceiptParseSpec';
            }
            // 传入通道的 receive_params 配置（若有），供 Node 端优先按 DB 规则解析收款列表
            if ($channelid > 0) {
                $prefix = C('DB_PREFIX') ?: 'tw_';
                $configTable = $prefix . 'paytype_config';
                $cfg = M()->table($configTable)->where(['channelid' => $channelid])->field('receive_params')->find();
                if ($cfg && !empty($cfg['receive_params'])) {
                    $rp = json_decode($cfg['receive_params'], true);
                    if (is_array($rp)) {
                        $args['receive_params'] = $rp;
                    }
                }
            }
        }
        if ($action === 'executePayment') {
            $logFile = $this->runtimePath . '/payment_debug.log';
            $payLog = function ($msg) use ($logFile) {
                @file_put_contents($logFile, date('Y-m-d H:i:s') . ' ' . $msg . "\n", FILE_APPEND);
            };

            $prefix = C('DB_PREFIX') ?: 'tw_';
            $configTable = $prefix . 'paytype_config';
            $orderModel = D('ExchangeOrder');
            $payparamsId = isset($account['payparams_id']) ? (int)$account['payparams_id'] : 0;
            $channelid = isset($account['channelid']) ? (int)$account['channelid'] : 0;
            $bankcard = isset($account['appid']) ? trim((string)$account['appid']) : $username;

            $payLog('[executePayment] account=' . $username . ' channelid=' . $channelid . ' payparams_id=' . $payparamsId . ' bankcard=' . $bankcard);

            $paymentParams = null;
            if ($channelid > 0) {
                $cfg = M()->table($configTable)->where(['channelid' => $channelid])->field('payment_params')->find();
                if ($cfg && !empty($cfg['payment_params'])) {
                    $pp = json_decode($cfg['payment_params'], true);
                    if (is_array($pp) && !empty($pp['amount_selector']) && !empty($pp['submit_selector'])
                        && (!empty($pp['payee_selector']) || !empty($pp['bankcard_selector']))) {
                        $paymentParams = $pp;
                    }
                }
            }
            if ($paymentParams === null) {
                $payLog('[executePayment] payment_params=missing or invalid');
                return ['success' => false, 'error' => '通道未配置 payment_params 或必填选择器缺失，无法执行付款'];
            }
            $payLog('[executePayment] payment_params=loaded keys=' . implode(',', array_keys($paymentParams)));

            $orders = [];
            if ($payparamsId > 0 && $channelid > 0 && $orderModel) {
                $cutoff = time() - 86400; // 24 小时内
                $whereCond = [
                    'otype'         => 1,
                    'pay_channelid' => $channelid,
                    'payparams_id'  => $payparamsId,
                    'status'        => 1,
                    'task_status'   => 1,
                    'addtime >='    => $cutoff,
                ];
                $payLog('[order_query] where=' . json_encode($whereCond, JSON_UNESCAPED_UNICODE));
                $payLog('[order_query] otype=1 pay_channelid=' . $channelid . ' payparams_id=' . $payparamsId . ' status=1 task_status=1 addtime>=24h(' . $cutoff . ')');
                $tableName = (method_exists($orderModel, 'getTableName') ? $orderModel->getTableName() : (C('DB_PREFIX') ?: 'tw_') . 'exchange_order');
                $orderQuerySql = 'SELECT orderid,truename,bank,bankcard,mum,pay_proof FROM ' . $tableName
                    . ' WHERE otype=1 AND pay_channelid=' . intval($channelid) . ' AND payparams_id=' . intval($payparamsId)
                    . ' AND status=1 AND task_status=1 AND addtime>=' . intval($cutoff) . ' ORDER BY addtime ASC';
                $payLog('[order_query] sql=' . $orderQuerySql);
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
            $payLog('[order_query] count=' . count($orders) . ' orderids=' . implode(',', array_map(function ($o) { return isset($o['orderid']) ? $o['orderid'] : ''; }, $orders)));
            $payLog('[order_query] result=' . json_encode($orders, JSON_UNESCAPED_UNICODE));

            if (empty($orders)) {
                $payLog('[executePayment] no_orders skip');
                return ['success' => true, 'order_results' => [], 'summary' => '无待付款订单'];
            }

            foreach ($orders as $o) {
                $orderModel->where(['orderid' => $o['orderid']])->save([
                    'status'      => 2,
                    'task_status' => 2,
                ]);
            }
            $payLog('[order_update] set status=2 task_status=2 for ' . count($orders) . ' order(s)');

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
        if ($action === 'executePayment' && isset($payLog)) {
            $payLog('[node_call] orders_count=' . (isset($args['orders']) ? count($args['orders']) : 0));
        }
        $cmd = 'node ' . escapeshellarg($this->nodeScriptPath) . ' ' . escapeshellarg(json_encode($args)) . ' 2>&1';
        $output = shell_exec($cmd);
        if ($output === null || $output === '') {
            if (isset($payLog)) {
                $payLog('[node_call] output=empty');
            }
            return ['error' => 'Node script no output（常见原因：Node 需 18+，或 node 未在 PATH 中）'];
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
            if (isset($payLog)) {
                $payLog('[node_call] decode_fail lastLine_len=' . strlen($lastLine));
            }
            return ['raw' => $lastLine];
        }
        $decoded['_request_params'] = $args;

        if ($action === 'executePayment' && !empty($orders) && !empty($decoded['order_results'])) {
            if (isset($payLog)) {
                $payLog('[node_result] success=' . (isset($decoded['success']) ? ($decoded['success'] ? '1' : '0') : '?') . ' order_results_count=' . count($decoded['order_results']));
            }
            $this->_processPaymentOrderResults($decoded['order_results'], $orders, $account, isset($payLog) ? $payLog : null);
        }

        return $decoded;
    }

    /**
     * 根据 Node 返回的 order_results 更新订单状态、保存截图、确认订单（订单表用 D('ExchangeOrder')，支持分表）
     * @param callable|null $payLog 日志回调 function($msg)
     */
    protected function _processPaymentOrderResults($orderResults, $orders, $account, $payLog = null) {
        $orderModel = D('ExchangeOrder');
        if (!$orderModel) {
            if ($payLog) {
                $payLog('[processResults] orderModel=null');
            }
            return;
        }
        $bankcard = isset($account['appid']) ? trim((string)$account['appid']) : (isset($account['username']) ? $account['username'] : '');
        $saveDir = $this->runtimePath . '/pay_screenshots/' . date('Y/m/');
        if (!is_dir($saveDir)) {
            @mkdir($saveDir, 0755, true);
        }
        foreach ($orderResults as $res) {
            $orderid = isset($res['orderid']) ? $res['orderid'] : '';
            if ($orderid === '') {
                continue;
            }
            $success = !empty($res['success']);
            if ($payLog) {
                $payLog('[processResults] orderid=' . $orderid . ' success=' . ($success ? '1' : '0') . ($success ? '' : ' error=' . (isset($res['error']) ? $res['error'] : '')));
            }
            if ($success) {
                $proofPath = '';
                if (!empty($res['screenshot_base64'])) {
                    $ext = (strpos($res['screenshot_base64'], 'data:image/png') === 0) ? 'png' : 'jpg';
                    $filename = $orderid . '_' . time() . '.' . $ext;
                    $path = $saveDir . $filename;
                    $bin = base64_decode(preg_replace('/^data:image\/\w+;base64,/', '', $res['screenshot_base64']));
                    if ($bin !== false && file_put_contents($path, $bin) !== false) {
                        $proofPath = 'Runtime/pay_screenshots/' . date('Y/m/') . $filename;
                        if ($payLog) {
                            $payLog('[processResults] orderid=' . $orderid . ' screenshot_saved=' . $proofPath);
                        }
                    }
                }
                $row = $orderModel->where(['orderid' => $orderid])->find();
                $existing = isset($row['pay_proof']) ? trim((string)$row['pay_proof']) : '';
                $newProof = ($proofPath !== '' && $existing !== '') ? $existing . ',' . $proofPath : ($proofPath !== '' ? $proofPath : $existing);
                $orderModel->where(['orderid' => $orderid])->save([
                    'pay_proof'   => $newProof,
                    'status'      => 3,
                    'task_status' => 3,
                ]);
                if ($proofPath !== '') {
                    $orderInfo = $orderModel->where(['orderid' => $orderid])->find();
                    if ($orderInfo && $bankcard !== '') {
                        try {
                            $payCtrl = A('Pay/PayExchange');
                            $payCtrl->confirmC2COrderWithApiOrderInfo($orderInfo, $bankcard);
                            if ($payLog) {
                                $payLog('[processResults] orderid=' . $orderid . ' confirm_called=ok');
                            }
                        } catch (\Exception $e) {
                            if ($payLog) {
                                $payLog('[processResults] orderid=' . $orderid . ' confirm_exception=' . $e->getMessage());
                            }
                        }
                    }
                }
            } else {
                $orderModel->where(['orderid' => $orderid])->save([
                    'task_status' => 4,
                    'status'      => 8,
                ]);
            }
        }
    }

    protected function _readOpsDoc() {
        $default = ['pending_steps' => [], 'accounts' => []];
        if (!is_file($this->opsDocPath)) {
            return $default;
        }
        $json = file_get_contents($this->opsDocPath);
        $doc = json_decode($json, true);
        if (!is_array($doc)) {
            return $default;
        }
        if (!isset($doc['accounts']) || !is_array($doc['accounts'])) {
            $doc['accounts'] = [];
        }
        if (!isset($doc['pending_steps']) || !is_array($doc['pending_steps'])) {
            $doc['pending_steps'] = [];
        }
        return $doc;
    }

    protected function _appendStepToDoc(&$doc, $step) {
        if (!isset($doc['pending_steps'])) {
            $doc['pending_steps'] = [];
        }
        $doc['pending_steps'][] = $step;
        $this->_writeOpsDoc($doc);
    }

    protected function _writeStepResultToDoc($doc, $step, $result) {
        if (!isset($doc['steps_history'])) {
            $doc['steps_history'] = [];
        }
        $summary = isset($result['summary']) ? $result['summary'] : (isset($result['by_account']) ? 'by_account:' . count($result['by_account']) : '');
        if (isset($result['otp_result'])) {
            $summary .= ';otp_result=' . $result['otp_result'];
        }
        $doc['steps_history'][] = [
            'step' => $step,
            'result_summary' => $summary,
            'time' => date('Y-m-d H:i:s'),
        ];
        $doc['last_action'] = isset($step['action']) ? $step['action'] : '';
        $doc['last_result_summary'] = isset($result['summary']) ? $result['summary'] : (isset($result['by_account']) ? json_encode(array_keys($result['by_account'])) : '');
        if (!empty($doc['pending_steps']) && isset($doc['pending_steps'][0])) {
            array_shift($doc['pending_steps']);
        }
        if (!isset($doc['accounts']) || !is_array($doc['accounts'])) {
            $doc['accounts'] = [];
        }
        $byAccount = isset($result['by_account']) && is_array($result['by_account']) ? $result['by_account'] : [];
        if (!empty($byAccount)) {
            foreach ($byAccount as $u => $data) {
                if (!isset($doc['accounts'][$u]) || !is_array($doc['accounts'][$u])) {
                    $doc['accounts'][$u] = ['checked_receipts' => [], 'executed_payments' => []];
                }
                if (!isset($doc['accounts'][$u]['checked_receipts'])) {
                    $doc['accounts'][$u]['checked_receipts'] = [];
                }
                if (!isset($doc['accounts'][$u]['executed_payments'])) {
                    $doc['accounts'][$u]['executed_payments'] = [];
                }
                if (isset($data['checked_receipts']) && is_array($data['checked_receipts'])) {
                    foreach ($data['checked_receipts'] as $r) {
                        $doc['accounts'][$u]['checked_receipts'][] = $r;
                    }
                }
                if (!empty($data['payment_id'])) {
                    $doc['accounts'][$u]['executed_payments'][] = [
                        'id' => $data['payment_id'],
                        'amount' => isset($data['amount']) ? $data['amount'] : '',
                        'payee' => isset($data['payee']) ? $data['payee'] : '',
                        'time' => date('Y-m-d H:i:s'),
                    ];
                }
            }
        } else {
            $u = isset($result['_account']) ? $result['_account'] : '';
            if ($u !== '') {
                if (!isset($doc['accounts'][$u]) || !is_array($doc['accounts'][$u])) {
                    $doc['accounts'][$u] = ['checked_receipts' => [], 'executed_payments' => []];
                }
                if (!isset($doc['accounts'][$u]['checked_receipts'])) {
                    $doc['accounts'][$u]['checked_receipts'] = [];
                }
                if (!isset($doc['accounts'][$u]['executed_payments'])) {
                    $doc['accounts'][$u]['executed_payments'] = [];
                }
                if (isset($result['checked_receipts']) && is_array($result['checked_receipts'])) {
                    foreach ($result['checked_receipts'] as $r) {
                        $doc['accounts'][$u]['checked_receipts'][] = $r;
                    }
                }
                if (!empty($result['payment_id'])) {
                    $doc['accounts'][$u]['executed_payments'][] = [
                        'id' => $result['payment_id'],
                        'amount' => isset($result['amount']) ? $result['amount'] : '',
                        'payee' => isset($result['payee']) ? $result['payee'] : '',
                        'time' => date('Y-m-d H:i:s'),
                    ];
                }
            }
        }
        $doc['last_total_tokens'] = $this->totalTokens;
        $this->_writeOpsDoc($doc);
    }

    protected function _writeOpsDoc($doc) {
        $dir = dirname($this->opsDocPath);
        if (!is_dir($dir)) {
            @mkdir($dir, 0755, true);
        }
        file_put_contents($this->opsDocPath, json_encode($doc, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
    }

    /** 通道操作文档路径：Public/WebAutoScriptDoc/{channelid}.json */
    protected function _getChannelDocPath($channelid) {
        return $this->webAutoScriptDocDir . '/' . (int)$channelid . '.json';
    }

    /** 读取通道文档，无则返回 ['steps'=>[], 'token_used'=>0]；为旧步骤补全 position */
    protected function _readChannelDoc($channelid) {
        $path = $this->_getChannelDocPath($channelid);
        if (!is_file($path)) {
            return ['steps' => []];
        }
        $json = file_get_contents($path);
        $doc = json_decode($json, true);
        if (!is_array($doc) || !isset($doc['steps']) || !is_array($doc['steps'])) {
            return ['steps' => []];
        }
        if (!isset($doc['receipt_parse']) || !is_array($doc['receipt_parse'])) {
            $doc['receipt_parse'] = [];
        }
        foreach ($doc['steps'] as $i => $s) {
            if (!isset($s['position'])) {
                $doc['steps'][$i]['position'] = $i;
            }
        }
        return $doc;
    }

    /** 写入通道文档 */
    protected function _writeChannelDoc($channelid, $doc) {
        if (!is_dir($this->webAutoScriptDocDir)) {
            @mkdir($this->webAutoScriptDocDir, 0755, true);
        }
        $path = $this->_getChannelDocPath($channelid);
        file_put_contents($path, json_encode($doc, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
    }

    /**（已废弃）旧的默认步骤 seed 逻辑保留占位，实际不再使用，所有步骤均由文档/AI 决定 */
    protected function _seedDefaultStepsForChannel($channelType) {
        return [];
    }

    protected function _output($arr) {
        if (php_sapi_name() === 'cli') {
            echo json_encode($arr, JSON_UNESCAPED_UNICODE) . "\n";
        } else {
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode($arr, JSON_UNESCAPED_UNICODE);
        }
    }

    /**
     * 验证码表名（含前缀，配置中 DB_PREFIX 如 tw_）
     */
    protected function _getOtpTableName() {
        $prefix = defined('C') ? C('DB_PREFIX') : 'tw_';
        return $prefix . 'exchange_auto_payment_code';
    }

    /**
     * 获取并消费验证码（Node 轮询调用）
     * 请求：GET/POST account=xxx
     * 返回：{ "code": "12345" } 或 { "code": "" }，读到后即清空该条 code
     */
    public function getOtp() {
        $account = trim(I('request.account', '', 'strip_tags'));
        if ($account === '') {
            $this->_outputJson(['code' => '', 'error' => 'missing account']);
            return;
        }
        $table = $this->_getOtpTableName();
        $row = M()->table($table)->where(['account' => $account])->find();
        // 若该账号不存在记录，则自动插入一条 code 为空的记录，方便后台人工后续录入
        if (!$row) {
            M()->table($table)->add(['account' => $account, 'code' => '', 'addtime' => time()]);
            $this->_outputJson(['code' => '']);
            return;
        }
        $code = '';
        if (isset($row['code']) && trim($row['code']) !== '') {
            $code = trim($row['code']);
            M()->table($table)->where(['account' => $account])->save(['code' => '', 'addtime' => time()]);
        }
        $this->_outputJson(['code' => $code]);
    }

    /**
     * 保存验证码（后台人工录入后调用）
     * 请求：POST account=xxx&code=12345
     */
    public function setOtp() {
        $account = trim(I('post.account', '', 'strip_tags'));
        $code = trim(I('post.code', '', 'strip_tags'));
        if ($account === '') {
            $this->_outputJson(['ok' => false, 'error' => 'missing account']);
            return;
        }
        $table = $this->_getOtpTableName();
        $now = time();
        $exists = M()->table($table)->where(['account' => $account])->find();
        if ($exists) {
            M()->table($table)->where(['account' => $account])->save(['code' => $code, 'addtime' => $now]);
        } else {
            M()->table($table)->add(['account' => $account, 'code' => $code, 'addtime' => $now]);
        }
        $this->_outputJson(['ok' => true]);
    }

    /**
     * 图片验证码识别（AI 兜底）：接收 base64 图片，返回识别出的数字
     * 请求：POST image=base64 或 image_base64=xxx
     */
    public function recognizeCaptcha() {
        $image = I('post.image', '', '');
        if ($image === '') {
            $image = I('post.image_base64', '', '');
        }
        if ($image === '') {
            $this->_outputJson(['code' => '', 'error' => 'missing image']);
            return;
        }
        if (strpos($image, 'data:') === 0) {
            $image = preg_replace('/^data:image\/\w+;base64,/', '', $image);
        }
        // 兼容上游传输中残留的 URL 编码
        if (preg_match('/%[0-9A-Fa-f]{2}/', $image)) {
            $image = rawurldecode($image);
        }
        // 规范化 base64，避免 Gemini 报 Base64 decoding failed
        // 注意：x-www-form-urlencoded 可能把 '+' 还原成空格，必须先修复再去空白。
        $image = (string)$image;
        $image = str_replace(' ', '+', $image);
        $image = preg_replace('/[\r\n\t]/', '', $image);
        $bin = base64_decode($image, true);
        if ($bin === false) {
            // 兼容 URL-safe base64
            $imageTry = strtr($image, '-_', '+/');
            $mod = strlen($imageTry) % 4;
            if ($mod > 0) {
                $imageTry .= str_repeat('=', 4 - $mod);
            }
            $bin = base64_decode($imageTry, true);
            if ($bin !== false) {
                $image = $imageTry;
            }
        }
        if ($bin !== false) {
            // 重新编码为标准 base64，确保请求体可被 Gemini 正确解析
            $image = base64_encode($bin);
        } else {
            @file_put_contents(
                $this->runtimePath . '/captcha_recognize.log',
                date('Y-m-d H:i:s') . ' provider=precheck image_len=' . strlen($image) . ' code="" trace=invalid_base64' . "\n",
                FILE_APPEND
            );
            $this->_outputJson(['code' => '', 'error' => 'invalid image base64']);
            return;
        }
        $expectedLength = (int)I('post.expected_length', 0, 'intval');
        if ($expectedLength < 0) {
            $expectedLength = 0;
        }
        $colorHint = trim((string)I('post.color_hint', '', ''));
        $code = '';
        $provider = strtolower(trim((string)(C('AUTO_WEB_EXCHANGE_CAPTCHA_PROVIDER') ?: 'gemini')));
        $trace = [];
        $inputImagePath = '';
        // 保存提交给 AI 的图片，便于排查“模型实际看到的图”
        if ($bin !== false) {
            $dir = $this->runtimePath . '/captcha_inputs/' . date('Ymd');
            if (!is_dir($dir)) {
                @mkdir($dir, 0755, true);
            }
            $filename = date('His') . '_' . substr(md5($image . microtime(true)), 0, 10) . '.png';
            $fullPath = $dir . '/' . $filename;
            if (@file_put_contents($fullPath, $bin) !== false) {
                // 记录相对路径，日志更短更稳定
                $inputImagePath = 'Runtime/captcha_inputs/' . date('Ymd') . '/' . $filename;
            }
        }
        // 默认优先 Gemini；失败时回退旧实现（deepseek）
        if ($provider === 'gemini' || $provider === 'auto') {
            $g = $this->_recognizeCaptchaByGemini($image, $expectedLength, $colorHint);
            $trace[] = 'gemini:' . (!empty($g['ok']) ? 'ok' : 'fail')
                . (!empty($g['error']) ? (':' . $g['error']) : '')
                . (isset($g['finish_reason']) ? (':finish=' . $g['finish_reason']) : '')
                . (isset($g['raw_text']) && $g['raw_text'] !== '' ? (':raw=' . substr((string)$g['raw_text'], 0, 80)) : '');
            $code = isset($g['code']) ? trim((string)$g['code']) : '';
        }
        if ($code === '' && ($provider === 'deepseek' || $provider === 'auto' || $provider === 'gemini')) {
            $legacy = $this->_recognizeCaptchaByVision($image);
            $trace[] = 'legacy:' . ($legacy !== '' ? 'ok' : 'fail');
            $code = trim((string)$legacy);
        }
        @file_put_contents(
            $this->runtimePath . '/captcha_recognize.log',
            date('Y-m-d H:i:s')
            . ' provider=' . $provider
            . ' expected_length=' . $expectedLength
            . ' color_hint=' . $colorHint
            . ' image_len=' . strlen($image)
            . ' image_path=' . $inputImagePath
            . ' code="' . $code . '"'
            . ' trace=' . implode('|', $trace)
            . "\n",
            FILE_APPEND
        );
        $this->_outputJson(['code' => $code]);
    }

    /**
     * 收款列表解析规范：Node 在收款页选定 frame 并提交 URL + HTML 片段，由 AI 分析行/时间/金额等 selector。
     * 请求：POST JSON { "channelid": 601, "account_key": "...", "pageContext": { "url": "...", "html": "..." }, "force_refresh": true? }
     * 返回：{ "ok": true, "spec": { "row_selector": "...", "time_selector": "...", "amount_selector": "...", "row_id_selector"?: "...", "filters"?: { ... } } }
     */
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

    /**
     * Gemini 视觉识别验证码（推荐）。
     * 配置：
     * - AUTO_WEB_EXCHANGE_GEMINI_API_KEY（或 GEMINI_API_KEY）
     * - AUTO_WEB_EXCHANGE_GEMINI_MODEL（默认 gemini-2.0-flash）
     */
    protected function _recognizeCaptchaByGemini($base64Image, $expectedLength = 0, $colorHint = '') {
        $apiKey = trim((string)(C('AUTO_WEB_EXCHANGE_GEMINI_API_KEY') ?: C('GEMINI_API_KEY') ?: ''));
        if ($apiKey === '') {
            return ['ok' => false, 'code' => '', 'error' => 'missing_api_key'];
        }
        $model = trim((string)(C('AUTO_WEB_EXCHANGE_GEMINI_MODEL') ?: 'gemini-2.0-flash'));
        $url = 'https://generativelanguage.googleapis.com/v1beta/models/' . rawurlencode($model) . ':generateContent?key=' . urlencode($apiKey);
        $prompt = '请识别这张银行附加码图片里的验证码数字。只返回纯数字，不要任何其他文字。';
        if ((int)$expectedLength > 0) {
            $prompt .= '验证码长度为' . (int)$expectedLength . '位，若不是该位数请返回空字符串。';
        }
        if ($colorHint !== '') {
            $prompt .= '请优先识别' . $colorHint . '数字，忽略黑色/灰色干扰线和账号金额文字。';
        } else {
            $prompt .= '请忽略黑色/灰色干扰线和账号金额文字。';
        }
        if ((int)$expectedLength > 0) {
            $prompt .= '必须且只能返回' . (int)$expectedLength . '位数字。';
        }
        $prompt .= '若无法确定请返回空字符串。';
        $schemaPattern = ((int)$expectedLength > 0)
            ? ('^\\d{' . (int)$expectedLength . '}$')
            : '^\\d{4,6}$';
        $body = [
            'contents' => [[
                'parts' => [
                    ['text' => $prompt],
                    ['inline_data' => ['mime_type' => 'image/png', 'data' => $base64Image]],
                ],
            ]],
            'generationConfig' => [
                'temperature' => 0,
                // 提高输出预算，减少 finishReason=MAX_TOKENS 导致 content 为空
                'maxOutputTokens' => 64,
                'topP' => 1,
                // 强制结构化输出，降低“只返回1位数字”的概率
                'responseMimeType' => 'application/json',
                'responseSchema' => [
                    'type' => 'OBJECT',
                    'properties' => [
                        'code' => [
                            'type' => 'STRING',
                            'pattern' => $schemaPattern,
                        ],
                    ],
                    'required' => ['code'],
                ],
            ],
        ];
        $payload = json_encode($body, JSON_UNESCAPED_UNICODE);
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $payload,
            CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 25,
        ]);
        $res = curl_exec($ch);
        $err = curl_error($ch);
        $http = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($err || $res === false) {
            return ['ok' => false, 'code' => '', 'error' => 'curl:' . ($err ?: 'unknown')];
        }
        $data = json_decode($res, true);
        $finishReason = '';
        if (is_array($data) && !empty($data['candidates'][0]['finishReason'])) {
            $finishReason = (string)$data['candidates'][0]['finishReason'];
        }
        $text = '';
        if (is_array($data) && !empty($data['candidates'][0]['content']['parts']) && is_array($data['candidates'][0]['content']['parts'])) {
            foreach ($data['candidates'][0]['content']['parts'] as $p) {
                if (isset($p['text']) && trim((string)$p['text']) !== '') {
                    $text .= ' ' . trim((string)$p['text']);
                }
            }
            $text = trim($text);
        }
        $jsonCode = '';
        if ($text !== '') {
            $obj = json_decode($text, true);
            if (is_array($obj) && isset($obj['code'])) {
                $jsonCode = trim((string)$obj['code']);
            }
        }
        $digits = preg_replace('/\D/', '', ($jsonCode !== '' ? $jsonCode : (string)$text));
        if ($digits !== '') {
            if ((int)$expectedLength > 0) {
                if (preg_match('/^\d{' . (int)$expectedLength . '}$/', $digits)) {
                    return ['ok' => true, 'code' => $digits, 'http' => $http, 'finish_reason' => $finishReason, 'raw_text' => $text];
                }
                return ['ok' => false, 'code' => '', 'error' => 'non_' . (int)$expectedLength . '_digits:' . $digits, 'http' => $http, 'finish_reason' => $finishReason, 'raw_text' => $text];
            }
            if (preg_match('/^\d{4,6}$/', $digits)) {
                return ['ok' => true, 'code' => $digits, 'http' => $http, 'finish_reason' => $finishReason, 'raw_text' => $text];
            }
            return ['ok' => false, 'code' => '', 'error' => 'non_4_6_digits:' . $digits, 'http' => $http, 'finish_reason' => $finishReason, 'raw_text' => $text];
        }
        $sample = is_string($res) ? substr($res, 0, 800) : '';
        return ['ok' => false, 'code' => '', 'error' => 'empty_digits_http_' . $http . '_body_' . $sample, 'finish_reason' => $finishReason, 'raw_text' => $text];
    }

    protected function _recognizeCaptchaByVision($base64Image) {
        $url = 'https://api.deepseek.com/v1/chat/completions';
        $body = [
            'model' => 'deepseek-chat',
            'messages' => [
                [
                    'role' => 'user',
                    'content' => [
                        ['type' => 'text', 'text' => '这是转账页的验证码图片，请识别图中的数字验证码（通常为5位数字），只返回数字，不要任何其他文字或标点。'],
                        ['type' => 'image_url', 'image_url' => ['url' => 'data:image/png;base64,' . $base64Image]],
                    ],
                ],
            ],
            'max_tokens' => 32,
        ];
        $ch = curl_init($url);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => json_encode($body),
            CURLOPT_HTTPHEADER => ['Content-Type: application/json', 'Authorization: Bearer ' . self::DEEPSEEK_KEY],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 30,
        ]);
        $res = curl_exec($ch);
        curl_close($ch);
        if (!$res) {
            return '';
        }
        $data = json_decode($res, true);
        $text = isset($data['choices'][0]['message']['content']) ? trim($data['choices'][0]['message']['content']) : '';
        $text = preg_replace('/\D/', '', $text);
        return substr($text, 0, 8);
    }

    /**
     * 文档驱动：获取下一步。线性按 position；同位置多步按权重取最高；文档空或某位置全失败则调 AI（受该通道 token 上限限制）
     * 请求：POST JSON { "channelid": 601, "account_key": "...", "pageContext": { "url", "screenshot_base64"? }, "last_completed_step_id": "step_xxx" }
     */
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
            // 文档为空时仅依赖 AI 生成整段步骤，不再使用任何内置 seed
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
            // AI 也无法给出可用步骤时，认为该通道当前无法继续
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

    /** 在 steps 中取 position 等于 $pos 的步，按 weight 降序取第一条 */
    protected function _pickStepAtPosition($steps, $pos) {
        $at = [];
        foreach ($steps as $s) {
            if (isset($s['position']) && (int)$s['position'] === (int)$pos) {
                $at[] = $s;
            }
        }
        if (empty($at)) {
            return null;
        }
        usort($at, function ($a, $b) {
            $wa = isset($a['weight']) ? (int)$a['weight'] : 0;
            $wb = isset($b['weight']) ? (int)$b['weight'] : 0;
            return $wb - $wa;
        });
        return $at[0];
    }

    /**
     * 调 AI 生成步骤（文档空或某位置全失败）。扣该通道 token_used，超限不请求
     * @param int $position null=文档空生成整段，否则为该位置补充步骤
     * @return array 新步骤列表，含 id/position/weight
     */
    protected function _askAiForChannelSteps($channelid, &$doc, $channelType, $pageContext, $position) {
        $tokenUsed = $this->_getChannelRunTokenUsed($channelid);
        if ($tokenUsed >= self::TOKEN_LIMIT) {
            return [];
        }
        $url = isset($pageContext['url']) ? trim((string)$pageContext['url']) : '';
        $typeLabel = $channelType == 1 ? '付款' : '收款';
        if ($position === null) {
            // 文档为空：从当前页面开始规划完整流程（含必选 / 可选步骤），最后一步应为 viewReceipts/executePayment
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
            // 某个 position 的所有方案均失败：仅为该位置补充多个候选操作（同一 position），可包含可选步骤
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
        $res = $this->_callDeepSeekReturnUsage($messages);
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
        // 按通道类型与占位符规则过滤掉明显不合规的步骤
        $steps = $this->_filterChannelSteps($steps, $channelType);
        return $steps;
    }

    /**
     * 调 AI 生成收款列表解析规范（行/时间/金额 selector 等），按通道扣 token_used，超限不请求
     * @return array|null
     */
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
        $res = $this->_callDeepSeekReturnUsage($messages);
        if ($res === false) {
            return null;
        }
        $used = isset($res['usage']) ? (int)(isset($res['usage']['total_tokens']) ? $res['usage']['total_tokens'] : 0) : 0;
        if ($used <= 0 && isset($res['usage']['prompt_tokens']) && isset($res['usage']['completion_tokens'])) {
            $used = (int)$res['usage']['prompt_tokens'] + (int)$res['usage']['completion_tokens'];
        }
        $this->_addChannelRunTokenUsed($channelid, $used);
        $content = is_string($res['content']) ? trim($res['content']) : '';
        $spec = $this->_parseReceiptParseSpecJson($content);
        return $spec;
    }

    protected function _parseReceiptParseSpecJson($content) {
        $content = preg_replace('/^```\w*\s*|\s*```\s*$/s', '', $content);
        $content = trim($content);
        if ($content === '') {
            return null;
        }
        $spec = json_decode($content, true);
        if (!is_array($spec)) {
            $start = strpos($content, '{');
            $end = strrpos($content, '}');
            if ($start !== false && $end !== false && $end > $start) {
                $spec = json_decode(substr($content, $start, $end - $start + 1), true);
            }
        }
        if (!is_array($spec) || empty($spec['row_selector']) || empty($spec['amount_selector'])) {
            return null;
        }
        $out = [
            'row_selector' => (string)$spec['row_selector'],
            'time_selector' => isset($spec['time_selector']) ? (string)$spec['time_selector'] : '',
            'amount_selector' => (string)$spec['amount_selector'],
        ];
        if (!empty($spec['row_id_selector'])) {
            $out['row_id_selector'] = (string)$spec['row_id_selector'];
        }
        if (isset($spec['filters']) && is_array($spec['filters'])) {
            $out['filters'] = $spec['filters'];
        }
        return $out;
    }

    /**
     * 获取当前运行中某通道已使用的 AI token（仅本次 index 调用有效，不持久化）
     */
    protected function _getChannelRunTokenUsed($channelid) {
        $cid = (int)$channelid;
        return isset($this->channelRunTokens[$cid]) ? (int)$this->channelRunTokens[$cid] : 0;
    }

    /**
     * 为当前运行中某通道累计 AI token 用量
     */
    protected function _addChannelRunTokenUsed($channelid, $used) {
        $cid = (int)$channelid;
        $delta = (int)$used;
        if ($delta <= 0) {
            return;
        }
        if (!isset($this->channelRunTokens[$cid])) {
            $this->channelRunTokens[$cid] = 0;
        }
        $this->channelRunTokens[$cid] += $delta;
    }

    protected function _parseAiStepsJson($content, $position) {
        $content = preg_replace('/^```\w*\s*|\s*```\s*$/s', '', $content);
        $content = trim($content);
        $arr = json_decode($content, true);
        if (!is_array($arr)) {
            $start = strpos($content, '[');
            if ($start !== false) {
                $end = strrpos($content, ']');
                if ($end !== false && $end > $start) {
                    $arr = json_decode(substr($content, $start, $end - $start + 1), true);
                }
            }
        }
        if (!is_array($arr) || empty($arr)) {
            return [];
        }
        $steps = [];
        $pos = $position !== null ? (int)$position : 0;
        foreach ($arr as $i => $item) {
            if (!is_array($item) || empty($item['type'])) {
                continue;
            }
            $positionVal = isset($item['position']) ? (int)$item['position'] : ($position !== null ? (int)$position : $i);
            if ($position === null) {
                $pos = $positionVal;
            }
            $step = [
                'id'    => 'step_' . uniqid('', true),
                'type'  => $item['type'],
                'position' => $pos,
                'weight' => self::STEP_WEIGHT_INITIAL,
            ];
            if (!empty($item['selector'])) {
                $step['selector'] = $item['selector'];
            }
            if (isset($item['value'])) {
                $step['value'] = $item['value'];
            }
            if (isset($item['amount'])) {
                $step['amount'] = $item['amount'];
            }
            if (isset($item['payee'])) {
                $step['payee'] = $item['payee'];
            }
            if (isset($item['optional'])) {
                $step['optional'] = (bool)$item['optional'];
            } elseif (isset($item['required'])) {
                // 若 AI 提供 required 字段，则 optional = !required
                $step['optional'] = !((bool)$item['required']);
            }
            $steps[] = $step;
            if ($position === null) {
                $pos++;
            }
        }
        return $steps;
    }

    /**
     * 按通道类型与占位符规则过滤步骤：
     * - 登录用户名/密码步骤必须使用 {{username}}/{{password}} 占位符，否则丢弃
     * - 收款通道(2) 丢弃 executePayment；付款通道(1) 可选丢弃 viewReceipts（暂保留）
     */
    protected function _filterChannelSteps($steps, $channelType) {
        if (!is_array($steps) || empty($steps)) {
            return [];
        }
        $out = [];
        foreach ($steps as $step) {
            if (!is_array($step) || empty($step['type'])) {
                continue;
            }
            $type = strtolower((string)$step['type']);
            $selector = isset($step['selector']) ? (string)$step['selector'] : '';
            $value = isset($step['value']) ? (string)$step['value'] : null;

            $looksLikeUser = false;
            $looksLikePass = false;
            $looksLikeLoginClick = false;

            if ($selector !== '') {
                $selLower = mb_strtolower($selector, 'UTF-8');

                // 通道类型过滤：收款通道不允许明显的付款/转账相关步骤
                if ((int)$channelType === 2) {
                    // 显式禁止 executePayment 类型
                    if ($type === 'executepayment') {
                        continue;
                    }
                    // selector 中包含典型付款/收款输入字段名的步骤直接丢弃
                    $hasPayeeLike =
                        strpos($selLower, 'payee_') !== false ||          // PAYEE_ACCOUNT, PAYEE_ACCOUNT_NO 等
                        strpos($selLower, 'payeeaccount') !== false ||    // payeeAccount
                        strpos($selLower, 'tran_amt') !== false ||        // TRAN_AMT
                        strpos($selLower, 'amount') !== false;            // amount 字段

                    // 文案类中文关键词：收款方账号 / 收款账号 / 转账 / 付款 等
                    $hasCnPayLike =
                        mb_strpos($selLower, '收款方账号', 0, 'UTF-8') !== false ||
                        mb_strpos($selLower, '收款账号', 0, 'UTF-8') !== false ||
                        (
                            // 同时出现“收款”和“账号”基本可以判断是收款账号输入框
                            mb_strpos($selLower, '收款', 0, 'UTF-8') !== false &&
                            mb_strpos($selLower, '账号', 0, 'UTF-8') !== false
                        ) ||
                        mb_strpos($selLower, '转账', 0, 'UTF-8') !== false ||
                        mb_strpos($selLower, '付款', 0, 'UTF-8') !== false;

                    if ($hasPayeeLike || $hasCnPayLike) {
                        continue;
                    }
                }

                $looksLikeUser =
                    (strpos($selLower, 'userid') !== false) ||
                    (mb_strpos($selLower, '用户名', 0, 'UTF-8') !== false) ||
                    (mb_strpos($selLower, '账号', 0, 'UTF-8') !== false) ||
                    (mb_strpos($selLower, '户名', 0, 'UTF-8') !== false) ||
                    (strpos($selLower, 'loginname') !== false);
                $looksLikePass =
                    (strpos($selLower, 'loginpwd') !== false) ||
                    (strpos($selLower, 'loginpass') !== false) ||
                    (strpos($selLower, 'password') !== false) ||
                    (mb_strpos($selLower, '密码', 0, 'UTF-8') !== false);
                $looksLikeLoginClick =
                    ($type === 'click') && (
                        strpos($selLower, 'loginbutton') !== false ||
                        strpos($selLower, 'logonbutton') !== false ||
                        mb_strpos($selLower, '登录', 0, 'UTF-8') !== false
                    );
            }

            // 登录用户名步骤：明显是用户名/账号输入框时，value 必须是 {{username}}，并归类到 position 0
            if ($type === 'fill' && $looksLikeUser) {
                if ($value !== '{{username}}') {
                    continue;
                }
                $step['position'] = 0;
            }

            // 登录密码步骤：明显是密码输入框时，value 必须是 {{password}}，并归类到 position 1
            if ($type === 'fill' && $looksLikePass) {
                if ($value !== '{{password}}') {
                    continue;
                }
                $step['position'] = 1;
            }

            // 登录按钮点击：明显是登录按钮时，归类到 position 2
            if ($looksLikeLoginClick) {
                $step['position'] = 2;
            }

            // 收款通道中，viewReceipts 至少应在 position>=3
            if ($type === 'viewreceipts') {
                $pos = isset($step['position']) ? (int)$step['position'] : 0;
                if ($pos < 3) {
                    $step['position'] = 3;
                }
            }

            $out[] = $step;
        }
        return $out;
    }

    /** 调用 DeepSeek 并返回 [content, usage]，不累加 totalTokens（供按通道扣 token 用） */
    protected function _callDeepSeekReturnUsage($messages) {
        $body = json_encode([
            'model' => 'deepseek-chat',
            'messages' => $messages,
            'max_tokens' => 1024,
        ]);
        $ch = curl_init(self::DEEPSEEK_API);
        curl_setopt_array($ch, [
            CURLOPT_POST => true,
            CURLOPT_POSTFIELDS => $body,
            CURLOPT_HTTPHEADER => [
                'Content-Type: application/json',
                'Authorization: Bearer ' . self::DEEPSEEK_KEY,
            ],
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT => 60,
        ]);
        $response = curl_exec($ch);
        $err = curl_error($ch);
        curl_close($ch);
        if ($err || $response === false) {
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

    /**
     * 文档驱动：Node 执行完一步后调用，按 failure_type 更新步骤权重（成功+1/步骤失败-2/账号失败不更新），≤0 删步
     * 请求：POST JSON { "channelid": 601, "step": { "id": "step_xxx", "type": "...", ... }, "result": { ... }, "failure_type": "success"|"step_failure"|"account_failure" }
     */
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
            $this->runtimePath . '/report_step_result.log',
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
            @file_put_contents($this->runtimePath . '/report_step_result.log', date('Y-m-d H:i:s') . ' found=no steps_count=' . count($doc['steps']) . "\n", FILE_APPEND);
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
        @file_put_contents($this->runtimePath . '/report_step_result.log', date('Y-m-d H:i:s') . ' found=yes wrote_doc=yes' . "\n", FILE_APPEND);
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

    /** 返回登录页的 browserSteps（value 中 {{username}}/{{password}} 由 Node 替换） */
    protected function _getLoginBrowserSteps($account) {
        $steps = C('AUTO_WEB_EXCHANGE_LOGIN_BROWSER_STEPS');
        if (is_array($steps) && !empty($steps)) {
            return ['action' => 'browserSteps', 'steps' => $steps];
        }
        return [
            'action' => 'browserSteps',
            'steps' => [
                ['type' => 'fill', 'selector' => '#USERID, input[name="USERID"], input[placeholder*="用户名"], input[placeholder*="账号"]', 'value' => '{{username}}'],
                ['type' => 'fill', 'selector' => '#LOGINPWD, input[name="LOGINPWD"], input[type="password"]', 'value' => '{{password}}'],
                ['type' => 'click', 'selector' => 'input[value="登录"], button:has-text("登录"), a:has-text("登录"), input[type="submit"]'],
            ],
        ];
    }

    protected function _outputJson($arr) {
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode($arr, JSON_UNESCAPED_UNICODE);
    }
}
