<?php
namespace Cli\Controller;

use Think\Controller;

/**
 * Web / VMOS 自动交换共用：操作文档、通道 JSON、步骤解析与权重（无具体执行器）
 */
class ExchangeBaseController extends Controller {

    protected $totalTokens = 0;
    const TOKEN_LIMIT = 8000;
    const STEP_WEIGHT_INITIAL = 8;
    const STEP_WEIGHT_MAX = 10;
    const STEP_WEIGHT_SUCCESS_DELTA = 1;
    const STEP_WEIGHT_FAIL_DELTA = -2;

    /** 通道操作文档目录（子类构造中赋值）：如 Public/WebAutoScriptDoc 或 Public/AppAutoScriptDoc */
    protected $channelDocDir;
    protected $opsDocPath;
    protected $runtimePath;
    protected $channelRunTokens = [];

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

    protected function _getChannelDocPath($channelid) {
        return $this->channelDocDir . '/' . (int)$channelid . '.json';
    }

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

    protected function _writeChannelDoc($channelid, $doc) {
        if (!is_dir($this->channelDocDir)) {
            @mkdir($this->channelDocDir, 0755, true);
        }
        $path = $this->_getChannelDocPath($channelid);
        file_put_contents($path, json_encode($doc, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT));
    }

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
                $step['optional'] = !((bool)$item['required']);
            }
            $steps[] = $step;
            if ($position === null) {
                $pos++;
            }
        }
        return $steps;
    }

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

                if ((int)$channelType === 2) {
                    if ($type === 'executepayment') {
                        continue;
                    }
                    $hasPayeeLike =
                        strpos($selLower, 'payee_') !== false ||
                        strpos($selLower, 'payeeaccount') !== false ||
                        strpos($selLower, 'tran_amt') !== false ||
                        strpos($selLower, 'amount') !== false;

                    $hasCnPayLike =
                        mb_strpos($selLower, '收款方账号', 0, 'UTF-8') !== false ||
                        mb_strpos($selLower, '收款账号', 0, 'UTF-8') !== false ||
                        (
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

            if ($type === 'fill' && $looksLikeUser) {
                if ($value !== '{{username}}') {
                    continue;
                }
                $step['position'] = 0;
            }

            if ($type === 'fill' && $looksLikePass) {
                if ($value !== '{{password}}') {
                    continue;
                }
                $step['position'] = 1;
            }

            if ($looksLikeLoginClick) {
                $step['position'] = 2;
            }

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

    protected function _getChannelRunTokenUsed($channelid) {
        $cid = (int)$channelid;
        return isset($this->channelRunTokens[$cid]) ? (int)$this->channelRunTokens[$cid] : 0;
    }

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

    protected function _output($arr) {
        if (php_sapi_name() === 'cli') {
            echo json_encode($arr, JSON_UNESCAPED_UNICODE) . "\n";
        } else {
            header('Content-Type: application/json; charset=utf-8');
            echo json_encode($arr, JSON_UNESCAPED_UNICODE);
        }
    }

    protected function _outputJson($arr) {
        header('Content-Type: application/json; charset=utf-8');
        echo json_encode($arr, JSON_UNESCAPED_UNICODE);
    }
}
