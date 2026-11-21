import 'dart:async';
import 'dart:convert';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../models/transit_models.dart';

class LiveStreamFrame {
  const LiveStreamFrame({
    required this.isSimulated,
    required this.bannerMessage,
    required this.serverTsMs,
    required this.clockSkewMs,
    required this.vehicles,
  });

  final bool isSimulated;
  final String? bannerMessage;
  final int serverTsMs;
  final double clockSkewMs;
  final List<LiveVehicle> vehicles;
}

/// WebSocket client for /ws/live with automatic reconnect and server clock-skew estimation.
class LiveWsClient {
  LiveWsClient({required this.wsUrl});

  final String wsUrl;
  final _controller = StreamController<LiveStreamFrame>.broadcast();
  WebSocketChannel? _channel;
  Timer? _reconnectTimer;
  double _clockSkewMs = 0.0;
  bool _disposed = false;

  Stream<LiveStreamFrame> get stream => _controller.stream;
  double get clockSkewMs => _clockSkewMs;

  void connect() {
    if (_disposed) return;
    _channel?.sink.close();
    _channel = WebSocketChannel.connect(Uri.parse(wsUrl));
    _channel!.stream.listen(
      (raw) {
        final decoded = jsonDecode(raw as String) as Map<String, dynamic>;
        final serverTs = (decoded['server_ts_ms'] ?? DateTime.now().millisecondsSinceEpoch) as int;
        final localTs = DateTime.now().millisecondsSinceEpoch;
        final measuredSkew = (serverTs - localTs).toDouble();
        _clockSkewMs = _clockSkewMs == 0.0 ? measuredSkew : (0.7 * _clockSkewMs + 0.3 * measuredSkew);

        final rawVehicles = (decoded['vehicles'] as List<dynamic>? ?? []);
        final vehicles = rawVehicles
            .map((e) => LiveVehicle.fromJson(e as Map<String, dynamic>, serverTs))
            .toList();

        _controller.add(
          LiveStreamFrame(
            isSimulated: (decoded['is_simulated'] ?? false) as bool,
            bannerMessage: decoded['banner_message'] as String?,
            serverTsMs: serverTs,
            clockSkewMs: _clockSkewMs,
            vehicles: vehicles,
          ),
        );
      },
      onError: (_) => _scheduleReconnect(),
      onDone: _scheduleReconnect,
    );
  }

  void _scheduleReconnect() {
    if (_disposed) return;
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(const Duration(seconds: 3), connect);
  }

  void dispose() {
    _disposed = true;
    _reconnectTimer?.cancel();
    _channel?.sink.close();
    _controller.close();
  }
}
