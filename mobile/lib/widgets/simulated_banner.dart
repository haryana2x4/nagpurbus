import 'package:flutter/material.dart';
import '../models/transit_models.dart';

/// Mandatory global banner shown on EVERY screen whenever simulated data is active.
class SimulatedModeBanner extends StatelessWidget {
  const SimulatedModeBanner({
    super.key,
    required this.isSimulated,
    this.message,
  });

  final bool isSimulated;
  final String? message;

  @override
  Widget build(BuildContext context) {
    if (!isSimulated) return const SizedBox.shrink();
    return Semantics(
      liveRegion: true,
      label: 'Simulated developer mode active. Bus locations are not real NMC GPS.',
      child: Material(
        color: const Color(0xFF92400E),
        child: SafeArea(
          bottom: false,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
            child: Row(
              children: [
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                  decoration: BoxDecoration(
                    color: const Color(0xFFFEF3C7),
                    borderRadius: BorderRadius.circular(999),
                  ),
                  child: const Text(
                    'SIMULATED',
                    style: TextStyle(
                      color: Color(0xFF78350F),
                      fontSize: 10,
                      fontWeight: FontWeight.w800,
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    message ?? 'Dev mode simulated fleet — not real live NMC GPS',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Color(0xFFFEF3C7),
                      fontSize: 11.5,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Colour-blind-safe (Okabe-Ito derived) Truth-in-Data source badge required beside every time.
class DataSourceBadge extends StatelessWidget {
  const DataSourceBadge({super.key, required this.source});

  final DataSourceLabel source;

  Color get _bgColor {
    switch (source) {
      case DataSourceLabel.live:
        return const Color(0xFF009E73);
      case DataSourceLabel.predicted:
        return const Color(0xFF0072B2);
      case DataSourceLabel.scheduled:
        return const Color(0xFF1E3A8A);
      case DataSourceLabel.simulated:
        return const Color(0xFFB45309);
      case DataSourceLabel.noData:
        return const Color(0xFF475569);
    }
  }

  IconData get _icon {
    switch (source) {
      case DataSourceLabel.live:
        return Icons.sensors;
      case DataSourceLabel.predicted:
        return Icons.auto_graph;
      case DataSourceLabel.scheduled:
        return Icons.schedule;
      case DataSourceLabel.simulated:
        return Icons.science;
      case DataSourceLabel.noData:
        return Icons.signal_wifi_off;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: 'Data source: ${source.displayLabel}',
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 3),
        decoration: BoxDecoration(
          color: _bgColor,
          borderRadius: BorderRadius.circular(6),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(_icon, size: 11, color: Colors.white),
            const SizedBox(width: 4),
            Text(
              source.displayLabel,
              style: const TextStyle(
                color: Colors.white,
                fontSize: 10,
                fontWeight: FontWeight.w800,
                letterSpacing: 0.4,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
