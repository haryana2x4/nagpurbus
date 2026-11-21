import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:google_fonts/google_fonts.dart';
import 'models/transit_models.dart';
import 'services/live_ws_client.dart';
import 'widgets/simulated_banner.dart';

void main() {
  runApp(const NagpurBusApp());
}

class NagpurBusApp extends StatelessWidget {
  const NagpurBusApp({super.key});

  @override
  Widget build(BuildContext context) {
    final baseTextTheme = GoogleFonts.notoSansDevanagariTextTheme();
    return MaterialApp(
      title: 'NagpurBus',
      debugShowCheckedModeBanner: false,
      supportedLocales: const [Locale('en'), Locale('hi'), Locale('mr')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      themeMode: ThemeMode.system,
      theme: ThemeData(
        colorSchemeSeed: const Color(0xFF0072B2),
        useMaterial3: true,
        textTheme: baseTextTheme,
      ),
      darkTheme: ThemeData(
        brightness: Brightness.dark,
        colorSchemeSeed: const Color(0xFF0072B2),
        useMaterial3: true,
        textTheme: GoogleFonts.notoSansDevanagariTextTheme(ThemeData.dark().textTheme),
      ),
      home: const LiveMapShellScreen(),
    );
  }
}

class LiveMapShellScreen extends StatefulWidget {
  const LiveMapShellScreen({super.key});

  @override
  State<LiveMapShellScreen> createState() => _LiveMapShellScreenState();
}

class _LiveMapShellScreenState extends State<LiveMapShellScreen> {
  static const _wsUrl = String.fromEnvironment(
    'NAGPURBUS_WS_URL',
    defaultValue: 'ws://10.0.2.2:8010/ws/live',
  );

  late final LiveWsClient _wsClient;
  LiveStreamFrame? _latestFrame;

  @override
  void initState() {
    super.initState();
    _wsClient = LiveWsClient(wsUrl: _wsUrl)..connect();
    _wsClient.stream.listen((frame) {
      if (mounted) setState(() => _latestFrame = frame);
    });
  }

  @override
  void dispose() {
    _wsClient.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final frame = _latestFrame;
    return Scaffold(
      body: Column(
        children: [
          SimulatedModeBanner(
            isSimulated: frame?.isSimulated ?? false,
            message: frame?.bannerMessage,
          ),
          AppBar(
            title: const Text('NagpurBus'),
            actions: [
              Padding(
                padding: const EdgeInsets.only(right: 12),
                child: DataSourceBadge(
                  source: frame == null
                      ? DataSourceLabel.noData
                      : (frame.isSimulated ? DataSourceLabel.simulated : DataSourceLabel.live),
                ),
              ),
            ],
          ),
          Expanded(
            child: frame == null
                ? const Center(child: Text('Connecting to NagpurBus live stream...'))
                : ListView.builder(
                    itemCount: frame.vehicles.length,
                    itemBuilder: (context, idx) {
                      final v = frame.vehicles[idx];
                      return ListTile(
                        leading: CircleAvatar(child: Text(v.routeShortName)),
                        title: Text('${v.label} → ${v.headsign}'),
                        subtitle: Text(
                          'Next: ${v.nextStopName} · ${(v.speedMps * 3.6).round()} km/h',
                        ),
                        trailing: DataSourceBadge(source: v.source),
                      );
                    },
                  ),
          ),
          const Padding(
            padding: EdgeInsets.all(8),
            child: Text(
              '© OpenStreetMap contributors (ODbL 1.0)',
              style: TextStyle(fontSize: 11),
            ),
          ),
        ],
      ),
    );
  }
}
