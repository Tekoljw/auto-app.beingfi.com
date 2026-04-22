<?php
namespace Cli\Common;

/**
 * Web / VMOS 自动交换共用：签名、收款提交、付款订单结果处理（无浏览器/云机依赖）
 */
class ExchangeCommon {

    public static function createSign($md5key, $list) {
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
     * @param string $runtimePath Runtime 根目录绝对路径
     * @param array  $account     账户信息（含 username/channelid 等）
     * @param array  $receipts      Node/执行器返回的 receipts
     * @return array { submitted, skipped, summary }
     */
    public static function submitReceiptsForAccount($runtimePath, $account, $receipts) {
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

        $cfgRow = null;
        if ($channelid > 0) {
            $cfgRow = M()->table($configTable)->where(['channelid' => $channelid])->field('currencyid')->find();
        }

        $paramRow = M()->table($paramsTable)->where(['login_account' => $username])->field('userid,appid,task_status,task_success_time')->find();
        $taskStatus = $paramRow && isset($paramRow['task_status']) ? (int)$paramRow['task_status'] : 0;
        $taskTime   = $paramRow && isset($paramRow['task_success_time']) ? (int)$paramRow['task_success_time'] : 0;

        $now = time();
        if ($taskStatus === 2 && $taskTime > 0) {
            $startTs = $taskTime;
        } else {
            $startTs = $now - 21600;
        }

        $currency = 'MMK';
        if ($cfgRow && isset($cfgRow['currencyid']) && (int)$cfgRow['currencyid'] > 0) {
            $cid = (int)$cfgRow['currencyid'];
            $cRow = M()->table($currTable)->where(['id' => $cid])->field('currency')->find();
            if ($cRow && !empty($cRow['currency'])) {
                $currency = $cRow['currency'];
            }
        }

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

            $ts = strtotime($dateStr);
            if ($ts === false) {
                $result['skipped'][] = $r;
                continue;
            }

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
            $payload['sign'] = self::createSign($md5Key, $payload);

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

            $logLine = date('Y-m-d H:i:s') . ' account=' . $username . ' orderQueren request url=' . $orderUrl . ' payload=' . json_encode($payload, JSON_UNESCAPED_UNICODE) . ' response_http_code=' . $httpCode . ' response_body=' . (is_string($resp) ? $resp : '') . ' curl_error=' . $err . "\n";
            @file_put_contents($runtimePath . '/order_queren_submit.log', $logLine, FILE_APPEND);

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
     * @param callable|null $payLog
     */
    public static function processPaymentOrderResults($runtimePath, $orderResults, $orders, $account, $payLog = null) {
        $orderModel = D('ExchangeOrder');
        if (!$orderModel) {
            if ($payLog) {
                $payLog('[processResults] orderModel=null');
            }
            return;
        }
        $bankcard = isset($account['appid']) ? trim((string)$account['appid']) : (isset($account['username']) ? $account['username'] : '');
        $saveDir = $runtimePath . '/pay_screenshots/' . date('Y/m/');
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
}
