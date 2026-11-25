// riff 首期最小原生测试 App。
//
// 目的：用固定 posthog_flutter 6.0.0 在真实 Android/iOS 原生平台生成 Capture v1
// 协议样本，供 Pivot 桩与 #46 kind 复用。这里只使用合成主体（stub-a）、合成包名
// （com.example.riff.stub.android/ios）和合成属性，不采集真实账号或设备信息。
//
// 运行参数全部通过 --dart-define 注入，便于 GitHub Actions 在同一个 App 内切换
// 抓包地址与压缩方式：
//   CAPTURE_HOST  抓包代理地址（Android 模拟器为 http://10.0.2.2:8000）
//   COMPRESSION   gzip（默认）或 none
//   STUB_TOKEN    合成桩写入凭据
//   SUBJECT       合成主体标识
import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:posthog_flutter/posthog_flutter.dart';

const String captureHost = String.fromEnvironment(
  'CAPTURE_HOST',
  defaultValue: 'http://10.0.2.2:8000',
);
const String compressionMode = String.fromEnvironment(
  'COMPRESSION',
  defaultValue: 'gzip',
);
const String stubToken = String.fromEnvironment(
  'STUB_TOKEN',
  defaultValue: 'pivot_event_stub_example',
);
const String probeSubject = String.fromEnvironment(
  'SUBJECT',
  defaultValue: 'stub-a',
);

/// 验收要求的两个业务事件；抓包代理按名称识别业务批次。
const List<String> businessEvents = <String>[
  'onboarding_login_entry',
  'onboarding_login_result',
];

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const ProbeApp());
  // 不阻塞首帧，后台执行确定性的事件序列。
  unawaited(runProbe());
}

class ProbeApp extends StatelessWidget {
  const ProbeApp({super.key});

  @override
  Widget build(BuildContext context) {
    return const Directionality(
      textDirection: TextDirection.ltr,
      child: ColoredBox(
        color: Color(0xFFFFFFFF),
        child: Center(
          child: Text(
            'riff stub probe',
            textDirection: TextDirection.ltr,
            style: TextStyle(color: Color(0xFF000000), fontSize: 24),
          ),
        ),
      ),
    );
  }
}

void log(String message) {
  // ignore: avoid_print
  print('RIFF_PROBE $message');
}

Future<void> delaySeconds(int seconds) =>
    Future<void>.delayed(Duration(seconds: seconds));

/// 按“生命周期 → 身份画像 → 两个业务事件同批 → 重试窗口”的顺序驱动 SDK。
///
/// 每一步之间先 flush，保证生命周期批次、身份批次和业务批次互不混杂；业务批次
/// 由连续两次 capture 后一次 flush 组成。最后周期性 flush，让原生 SDK 在同一个
/// 进程内重发代理标记为 retry 的事件（SDK 的退避重试由队列调度）。
Future<void> runProbe() async {
  final config = PostHogConfig(stubToken)
    ..host = captureHost
    ..captureApplicationLifecycleEvents = true
    ..flushAt = 20
    ..flushInterval = const Duration(seconds: 30)
    ..compression = compressionMode == 'none'
        ? PostHogCompression.none
        : PostHogCompression.gzip
    ..debug = true;

  log('setup host=$captureHost compression=$compressionMode');
  await Posthog().setup(config);
  // 公共整数字段：Pivot 质量策略要求事件带 user_generation。
  await Posthog().register('user_generation', 1);

  try {
    // 1) 生命周期事件单独成批（Installed/Updated 由原生安装集成发出，Opened 由
    // 生命周期集成发出）。
    await delaySeconds(4);
    await Posthog().flush();
    log('lifecycle batch flushed');

    // 2) 身份关联与画像更新：$identify 携带 $set / $set_once。
    await delaySeconds(4);
    await Posthog().identify(
      userId: probeSubject,
      userProperties: const <String, Object>{'membership': 'plus'},
      userPropertiesSetOnce: const <String, Object>{'channel': 'organic'},
    );
    await delaySeconds(2);
    await Posthog().flush();
    log('identity batch flushed');

    // 3) 两个业务事件同批：先清空队列，再连续 capture 后立即 flush。
    await delaySeconds(3);
    await Posthog().capture(
      eventName: businessEvents[0],
      properties: const <String, Object>{
        'action': 'selected',
        'method': 'guest',
        'has_previous_account': false,
        'terms_accepted': true,
      },
    );
    await Posthog().capture(
      eventName: businessEvents[1],
      properties: const <String, Object>{
        'oauthType': 'guest',
        'is_new_register': true,
        'result': 'entered_home',
      },
    );
    await delaySeconds(1);
    await Posthog().flush();
    log('business batch flushed');

    // 4) 重试窗口：原生 SDK 把 retry 事件留在队列并按退避重发，周期 flush 加速
    // 这一过程，同时不引入额外事件。
    for (var i = 0; i < 6; i++) {
      await delaySeconds(5);
      await Posthog().flush();
    }
    log('retry window done');
  } catch (error) {
    log('probe failed: $error');
  }
  log('probe done; keep the process alive for background');
}
