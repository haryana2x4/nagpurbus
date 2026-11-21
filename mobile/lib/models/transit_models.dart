import 'dart:math' as math;

/// Mandatory Truth-in-Data source classification for every displayed time or position.
enum DataSourceLabel {
  live,
  predicted,
  scheduled,
  simulated,
  noData;

  static DataSourceLabel parse(String raw) {
    switch (raw.toUpperCase()) {
      case 'LIVE':
        return DataSourceLabel.live;
      case 'PREDICTED':
        return DataSourceLabel.predicted;
      case 'SCHEDULED':
        return DataSourceLabel.scheduled;
      case 'SIMULATED':
        return DataSourceLabel.simulated;
      default:
        return DataSourceLabel.noData;
    }
  }

  String get displayLabel {
    switch (this) {
      case DataSourceLabel.live:
        return 'LIVE';
      case DataSourceLabel.predicted:
        return 'PREDICTED';
      case DataSourceLabel.scheduled:
        return 'SCHEDULED';
      case DataSourceLabel.simulated:
        return 'SIMULATED';
      case DataSourceLabel.noData:
        return 'NO LIVE DATA';
    }
  }
}

class LiveVehicle {
  const LiveVehicle({
    required this.vehicleId,
    required this.label,
    required this.propulsionType,
    required this.routeId,
    required this.routeShortName,
    required this.routeColorHex,
    required this.mode,
    required this.directionId,
    required this.headsign,
    required this.shapeId,
    required this.lat,
    required this.lon,
    required this.sMeters,
    required this.speedMps,
    required this.bearingDeg,
    required this.nextStopName,
    required this.etaNextStopSec,
    required this.isDwelling,
    required this.crowdingLevel,
    required this.pingAgeSec,
    required this.source,
    required this.isSimulated,
    required this.anchorServerMs,
  });

  final String vehicleId;
  final String label;
  final String propulsionType;
  final String routeId;
  final String routeShortName;
  final String routeColorHex;
  final String mode;
  final int directionId;
  final String headsign;
  final String shapeId;
  final double lat;
  final double lon;
  final double sMeters;
  final double speedMps;
  final double bearingDeg;
  final String nextStopName;
  final int etaNextStopSec;
  final bool isDwelling;
  final int? crowdingLevel;
  final double pingAgeSec;
  final DataSourceLabel source;
  final bool isSimulated;
  final int anchorServerMs;

  factory LiveVehicle.fromJson(Map<String, dynamic> json, int serverTsMs) {
    return LiveVehicle(
      vehicleId: json['vehicle_id'] as String,
      label: (json['label'] ?? '') as String,
      propulsionType: (json['propulsion_type'] ?? 'unknown') as String,
      routeId: (json['route_id'] ?? '') as String,
      routeShortName: (json['route_short_name'] ?? '') as String,
      routeColorHex: (json['route_color'] ?? '1D4ED8') as String,
      mode: (json['mode'] ?? 'bus') as String,
      directionId: (json['direction_id'] ?? 0) as int,
      headsign: (json['headsign'] ?? '') as String,
      shapeId: (json['shape_id'] ?? '') as String,
      lat: ((json['true_lat'] ?? json['lat']) as num).toDouble(),
      lon: ((json['true_lon'] ?? json['lon']) as num).toDouble(),
      sMeters: ((json['s_m'] ?? 0) as num).toDouble(),
      speedMps: ((json['speed_mps'] ?? 0) as num).toDouble(),
      bearingDeg: ((json['bearing_deg'] ?? 0) as num).toDouble(),
      nextStopName: (json['next_stop_name'] ?? '') as String,
      etaNextStopSec: (json['eta_next_stop_s'] ?? 0) as int,
      isDwelling: (json['is_dwelling'] ?? false) as bool,
      crowdingLevel: json['crowding_level'] as int?,
      pingAgeSec: ((json['ping_age_s'] ?? 0) as num).toDouble(),
      source: DataSourceLabel.parse((json['source'] ?? 'NO_DATA') as String),
      isSimulated: (json['is_simulated'] ?? false) as bool,
      anchorServerMs: serverTsMs,
    );
  }
}

class DeadReckonedPose {
  const DeadReckonedPose({
    required this.lat,
    required this.lon,
    required this.bearingDeg,
  });

  final double lat;
  final double lon;
  final double bearingDeg;
}

/// Pure metric polyline dead-reckoning interpolator for smooth 60fps marker motion between 5s pings.
class RouteShapePolyline {
  const RouteShapePolyline({
    required this.shapeId,
    required this.totalLengthMeters,
    required this.points, // List of [lat, lon, s_m]
  });

  final String shapeId;
  final double totalLengthMeters;
  final List<List<double>> points;

  DeadReckonedPose interpolateAtS(double sTarget) {
    if (points.length < 2) {
      return const DeadReckonedPose(lat: 21.1458, lon: 79.0882, bearingDeg: 0);
    }
    final s = sTarget.clamp(0.0, totalLengthMeters);
    var lo = 0;
    var hi = points.length - 1;
    while (lo < hi - 1) {
      final mid = (lo + hi) >> 1;
      if (points[mid][2] <= s) {
        lo = mid;
      } else {
        hi = mid;
      }
    }
    final p0 = points[lo];
    final p1 = points[lo + 1];
    final ds = p1[2] - p0[2];
    final frac = ds > 0.01 ? (s - p0[2]) / ds : 0.0;
    final lat = p0[0] + frac * (p1[0] - p0[0]);
    final lon = p0[1] + frac * (p1[1] - p0[1]);
    final dLat = p1[0] - p0[0];
    final dLon = (p1[1] - p0[1]) * math.cos(lat * math.pi / 180.0);
    final bearing = (math.atan2(dLon, dLat) * 180.0 / math.pi + 360.0) % 360.0;
    return DeadReckonedPose(lat: lat, lon: lon, bearingDeg: bearing);
  }
}
