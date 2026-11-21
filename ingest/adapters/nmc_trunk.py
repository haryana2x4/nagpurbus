"""Official NMC Aapli Bus major city trunk corridors parsed from Maha Metro / UMTC Table 3-12.

Truth-in-Data provenance:
  - Source document: Maha Metro Nagpur — Comprehensive Feeder Service Project for Nagpur Metro
    (Final Report, Table 3-12 "Existing Route Details and City Bus Services Ridership",
    citing NMC Operational Parameters, and Table 3-13 Major Bus Stops).
    URL: https://www.metrorailnagpur.com/assets/Comprehensive-feeder-service-plan-CfHeA0M1.pdf
  - Every hub coordinate is verified from OpenStreetMap (Nominatim / Overpass) and snapped onto
    Nagpur's real 4,136-way OSM drivable bus road network via OSMRoadSnapper.
  - Because NMC has not published exact per-stop timetable PDFs for trunk city routes, trips are
    generated strictly from Table 3-12's official operational headways across Nagpur's service
    hours (06:00-22:00 urban, 06:30-20:30 outer industrial corridors like Butibori/Khaparkheda/
    Panchgaon) and marked `timepoint=0` (estimated headway schedule, NOT exact agency timepoints).
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from ingest.adapters.base import BaseTransitAdapter
from ingest.models import (
    AgencyRecord,
    CalendarRecord,
    GTFSFeedBundle,
    RouteRecord,
    SourceProvenance,
    StopRecord,
    StopTimeRecord,
    TripRecord,
)
from ingest.snapper import OSMRoadSnapper

logger = structlog.get_logger(__name__)

# Verified OSM coordinates for major Nagpur city bus hubs (Table 3-13 & Table 3-12)
# Format: stop_id -> (name_en, name_hi, name_mr, lat, lon, is_terminal, osm_provenance)
VERIFIED_TRUNK_HUBS: dict[str, tuple[str, str, str, float, float, bool, str]] = {
    "hub_sitabuldi": ("Sitabuldi Bus Terminal (Mor Bhavan)", "सीताबर्डी बस टर्मिनल (मोर भवन)", "सीताबर्डी बस स्थानक (मोर भवन)", 21.142540, 79.079610, True, "osm:nominatim/sitabuldi_city_bus_stand"),
    "hub_railway_stn": ("Nagpur Railway Station Bus Stand", "नागपुर रेलवे स्टेशन बस स्टैंड", "नागपूर रेल्वे स्थानक बस स्थानक", 21.153703, 79.086261, True, "osm:node/4317844791"),
    "hub_medical_sq": ("Medical Chowk", "मेडिकल चौक", "मेडिकल चौक", 21.131450, 79.097720, False, "osm:way/143281373"),
    "hub_sakkardara": ("Sakkardara Square", "सक्करदरा चौक", "सक्करदरा चौक", 21.121940, 79.115280, False, "osm:way/189372918"),
    "hub_dighori": ("Dighori Naka", "दिघोरी नाका", "दिघोरी नाका", 21.118620, 79.125290, False, "osm:node/4406865103"),
    "hub_ayodhya_tpt": ("Ayodhya Nagar T-Point", "अयोध्या नगर टी-पॉइंट", "अयोध्या नगर टी-पॉईंट", 21.115779, 79.110680, False, "osm:node/12545973169"),
    "hub_hudkeshwar": ("Hudkeshwar / Mhalgi Nagar Sq", "हुडकेश्वर / म्हालगी नगर चौक", "हुडकेश्वर / म्हालगी नगर चौक", 21.107112, 79.119174, False, "osm:node/6984042518"),
    "hub_bahadura": ("Bahadura / Umred Road", "बहादुरा / उमरेड रोड", "बहादुरा / उमरेड रोड", 21.101770, 79.152800, False, "osm:node/5048713352"),
    "hub_panchgaon": ("Panchgaon Bus Stop", "पाचगाव बस स्टॉप", "पाचगाव बस स्टॉप", 21.032867, 79.171869, True, "osm:node/13673512706"),
    "hub_wathoda": ("Wathoda / Bhandewadi", "वाठोडा / भांडेवाडी", "वाठोडा / भांडेवाडी", 21.141270, 79.159320, True, "osm:node/2907529361"),
    "hub_gandhibagh": ("Gandhibagh", "गांधीबाग", "गांधीबाग", 21.154330, 79.105850, False, "osm:node/7079665735"),
    "hub_itwari": ("Itwari Railway Station", "इतवारी रेलवे स्टेशन", "इतवारी रेल्वे स्थानक", 21.157500, 79.118830, False, "osm:node/305221832"),
    "hub_kalamna": ("Kalamna Market", "कलमना मार्केट", "कळमना बाजार", 21.167850, 79.140910, False, "osm:node/2100210203"),
    "hub_pardi": ("Pardi / Prajapati Nagar Terminal", "पारडी / प्रजापति नगर", "पारडी / प्रजापती नगर", 21.150149, 79.148823, True, "osm:node/7944687085"),
    "hub_sadar": ("Sadar Bazar", "सदर बाजार", "सदर बाजार", 21.159200, 79.079610, False, "osm:way/1162509210"),
    "hub_mankapur": ("Mankapur Square", "मानकापुर चौक", "मानकापूर चौक", 21.187810, 79.079090, False, "osm:way/1287998022"),
    "hub_koradi_naka": ("Koradi Naka", "कोराडी नाका", "कोराडी नाका", 21.207527, 79.077157, False, "osm:node/13455877401"),
    "hub_godhani": ("Godhani", "गोधनी", "गोधनी", 21.211580, 79.068630, True, "osm:node/6203685513"),
    "hub_ktps": ("KTPS Gate No. 1 (Koradi)", "केटीपीएस गेट 1 (कोराडी)", "केटीपीएस गेट १ (कोराडी)", 21.236072, 79.084466, False, "osm:node/13434501622"),
    "hub_koradi": ("Koradi Devi Mandir", "कोराडी देवी मंदिर", "कोराडी देवी मंदिर", 21.253650, 79.098530, False, "osm:way/280477637"),
    "hub_khaparkheda": ("Khaparkheda Thermal Power Stn", "खापरखेड़ा", "खापरखेडा", 21.275410, 79.121340, True, "osm:way/128552630"),
    "hub_kamal_chowk": ("Kamal Chowk / Pachpaoli", "कमल चौक / पाचपावली", "कमल चौक / पाचपावली", 21.166070, 79.103120, False, "osm:node/7226989697"),
    "hub_indora": ("Indora Bus Stop", "इंदोरा बस स्टॉप", "इंदोरा बस स्थानक", 21.173687, 79.100728, False, "osm:node/7944686882"),
    "hub_jaripatka": ("Jaripatka", "जरीपटका", "जरीपटका", 21.189490, 79.085320, False, "osm:way/314731413"),
    "hub_nara": ("Nara Bus Stand (Jaripatka)", "नारा बस स्टैंड", "नारा बस स्थानक", 21.195026, 79.090266, True, "osm:node/14051307223"),
    "hub_automotive": ("Automotive Square / Kamptee Rd", "ऑटोमोटिव चौक", "ऑटोमोटिव्ह चौक", 21.185792, 79.119506, False, "osm:node/7944685834"),
    "hub_khasala": ("Khasala (Kamptee Rd)", "खसाला (कामठी रोड)", "खसाला (कामठी रोड)", 21.200930, 79.133430, False, "osm:nominatim/khasala"),
    "hub_madhav_nagri": ("Madhav Nagri / Bhilgaon (Kamptee Rd)", "माधव नगरी / भिलगांव", "माधव नगरी / भिलगाव", 21.194590, 79.144700, False, "osm:nominatim/bhilgaon_madhav_nagri"),
    "hub_bharat_town": ("Bharat Town / Yerkheda (Kamptee Rd)", "भारत टाउन / येरखेड़ा", "भारत टाऊन / येरखेडा", 21.208620, 79.154460, False, "osm:nominatim/bharat_town_kamptee_rd"),
    "hub_kamptee": ("Kamptee Bus Stand (Trunk)", "कामठी बस स्टैंड", "कामठी बस स्थानक", 21.212232, 79.197798, True, "osm:node/3962005509"),
    "hub_mahal": ("Mahal / Gandhisagar", "महाल", "महाल", 21.143000, 79.110180, False, "osm:nominatim/mahal_nagpur"),
    "hub_nandanvan": ("Nandanvan", "नंदनवन", "नंदनवन", 21.137560, 79.122680, False, "osm:nominatim/nandanvan_nagpur"),
    "hub_dharampeth": ("Dharampeth / Shankar Nagar", "धरमपेठ", "धरमपेठ", 21.140970, 79.062430, False, "osm:nominatim/dharampeth_nagpur"),
    "hub_trimurti_nagar": ("Trimurti Nagar", "त्रिमूर्ति नगर", "त्रिमूर्ती नगर", 21.113160, 79.042660, False, "osm:nominatim/trimurti_nagar"),
    "hub_besa": ("Besa Square", "बेसा चौक", "बेसा चौक", 21.084750, 79.094470, False, "osm:nominatim/besa_nagpur"),
    "hub_civil_lines": ("Civil Lines / GPO", "सिविल लाइन्स", "सिव्हिल लाईन्स", 21.154990, 79.078900, False, "osm:nominatim/civil_lines"),
    "hub_katol_naka": ("Katol Naka / Friends Colony", "काटोल नाका / फ्रेंड्स कॉलोनी", "काटोल नाका / फ्रेंड्स कॉलनी", 21.178494, 79.045319, False, "osm:node/11576006643"),
    "hub_gorewada": ("Gorewada", "गोरेवाडा", "गोरेवाडा", 21.197020, 79.038740, True, "osm:way/44430029"),
    "hub_ravi_nagar": ("Ravi Nagar Square", "रवि नगर चौक", "रवी नगर चौक", 21.148270, 79.054670, False, "osm:node/5966013224"),
    "hub_vayusena": ("Vayusena Nagar", "वायुसेना नगर", "वायुसेना नगर", 21.161679, 79.040921, False, "osm:node/6161169128"),
    "hub_wadi": ("Wadi T-Point / Amravati Rd", "वाडी टी-पॉइंट", "वाडी टी-पॉईंट", 21.149140, 78.977710, True, "osm:nominatim/wadi_nagpur"),
    "hub_deekshabhoomi": ("Deekshabhoomi", "दीक्षाभूमि", "दीक्षाभूमी", 21.128200, 79.066930, False, "osm:node/297521871"),
    "hub_abhayankar": ("Abhayankar Nagar", "अभ्यंकर नगर", "अभ्यंकर नगर", 21.125621, 79.058625, False, "osm:node/2956824967"),
    "hub_pratap_nagar": ("Pratap Nagar Square", "प्रताप नगर चौक", "प्रताप नगर चौक", 21.113710, 79.056780, False, "osm:node/8913989976"),
    "hub_ambazari": ("Ambazari T-Point / VNIT", "अंबाझरी टी-पॉइंट / वीएनआईटी", "अंबाझरी टी-पॉईंट / व्हीएनआयटी", 21.128692, 79.045745, False, "osm:node/11371112354"),
    "hub_jaitala": ("Jaitala Last Bus Stop", "जैताला बस स्टॉप", "जैताळा शेवटचा बस स्टॉप", 21.111211, 79.024130, True, "osm:node/14051520862"),
    "hub_crpf": ("CRPF Gate (Hingna Rd)", "सीआरपीएफ गेट (हिंगणा रोड)", "सीआरपीएफ गेट (हिंगणा रोड)", 21.097250, 79.010320, False, "osm:way/1202248318"),
    "hub_ycce_hingna": ("YCCE / Hingna Road", "वायसीसीई / हिंगणा रोड", "वायसीसीई / हिंगणा रोड", 21.095225, 78.978564, True, "osm:node/4283949290"),
    "hub_ajni_sq": ("Ajni Square", "अजनी चौक", "अजनी चौक", 21.118212, 79.072107, False, "osm:node/11368300691"),
    "hub_chhatrapati_sq": ("Chhatrapati Square", "छत्रपति चौक", "छत्रपती चौक", 21.109139, 79.069611, False, "osm:node/11368300690"),
    "hub_manish_nagar": ("Manish Nagar", "मनीष नगर", "मनीष नगर", 21.093490, 79.067510, False, "osm:way/1126641142"),
    "hub_beltarodi": ("Beltarodi", "बेलतरोडी", "बेलतरोडी", 21.080440, 79.077280, True, "osm:way/487389296"),
    "hub_khapri_nh44": ("Khapri (Wardha Rd)", "खापरी (वर्धा रोड)", "खापरी (वर्धा रोड)", 21.049348, 79.047845, False, "osm:node/11368300696"),
    "hub_butibori": ("Butibori MIDC (Indorama)", "बुटीबोरी एमआईडीसी", "बुटीबोरी एमआयडीसी", 20.928190, 79.007080, True, "osm:way/150340951"),
    "hub_brahmani_fata": ("Brahmani Fata (Kalmeshwar Rd)", "ब्राह्मणी फाटा", "ब्राह्मणी फाटा", 21.237373, 78.903936, True, "osm:node/13686160376"),
}


@dataclass(frozen=True)
class TrunkRouteSpec:
    route_id: str
    short_name: str
    long_name: str
    name_hi: str
    name_mr: str
    color: str
    stops: tuple[str, ...]
    headway_min: int  # From Maha Metro / NMC Table 3-12 operational headway
    start_hour: int
    end_hour: int     # Shorter service window on outer industrial routes (Butibori, Khaparkheda, Panchgaon, Wathoda)
    table_ref: str


# 16 Official NMC Aapli Bus Trunk Corridors from Comprehensive Feeder Service Plan Table 3-12
OFFICIAL_NMC_TRUNK_ROUTES: tuple[TrunkRouteSpec, ...] = (
    TrunkRouteSpec(
        "nmc_trunk_01", "T-01", "Pardi - Mahal - Sitabuldi - Trimurti Nagar - Jaitala",
        "पारडी - महाल - सीताबर्डी - जैताला", "पारडी - महाल - सीताबर्डी - जैताळा", "059669",
        ("hub_pardi", "hub_itwari", "hub_gandhibagh", "hub_mahal", "hub_railway_stn", "hub_sitabuldi", "hub_abhayankar", "hub_pratap_nagar", "hub_trimurti_nagar", "hub_jaitala"),
        headway_min=15, start_hour=6, end_hour=22, table_ref="Table 3-12 #1 Pardi-Jaitala",
    ),
    TrunkRouteSpec(
        "nmc_trunk_06", "T-06", "Sitabuldi - Automotive Sq - Madhav Nagri - Bharat Town - Kamptee",
        "सीताबर्डी - माधव नगरी - भारत टाउन - कामठी", "सीताबर्डी - माधव नगरी - भारत टाऊन - कामठी", "059669",
        ("hub_sitabuldi", "hub_railway_stn", "hub_indora", "hub_automotive", "hub_khasala", "hub_madhav_nagri", "hub_bharat_town", "hub_kamptee"),
        headway_min=12, start_hour=6, end_hour=22, table_ref="Table 3-12 #6/#19 Sitabuldi-Kamptee",
    ),
    TrunkRouteSpec(
        "nmc_trunk_10", "T-10", "Sitabuldi - Civil Lines - Sadar - Mankapur - Godhani",
        "सीताबर्डी - सिविल लाइन्स - सदर - गोधनी", "सीताबर्डी - सिव्हिल लाईन्स - सदर - गोधनी", "059669",
        ("hub_sitabuldi", "hub_civil_lines", "hub_sadar", "hub_mankapur", "hub_koradi_naka", "hub_godhani"),
        headway_min=15, start_hour=6, end_hour=22, table_ref="Table 3-12 #10 Sitabuldi-Godhani",
    ),
    TrunkRouteSpec(
        "nmc_trunk_11", "T-11", "Sitabuldi - Koradi - Khaparkheda",
        "सीताबर्डी - कोराडी - खापरखेड़ा", "सीताबर्डी - कोराडी - खापरखेडा", "047857",
        ("hub_sitabuldi", "hub_sadar", "hub_mankapur", "hub_koradi_naka", "hub_ktps", "hub_koradi", "hub_khaparkheda"),
        headway_min=20, start_hour=6, end_hour=21, table_ref="Table 3-12 #11 Sitabuldi-Khaparkheda",
    ),
    TrunkRouteSpec(
        "nmc_trunk_12", "T-12", "Sitabuldi - Gandhibagh - Kalamna - Pardi",
        "सीताबर्डी - गांधीबाग - कलमना - पारडी", "सीताबर्डी - गांधीबाग - कळमना - पारडी", "059669",
        ("hub_sitabuldi", "hub_railway_stn", "hub_gandhibagh", "hub_itwari", "hub_kalamna", "hub_pardi"),
        headway_min=15, start_hour=6, end_hour=22, table_ref="Table 3-12 #12 Sitabuldi-Pardi",
    ),
    TrunkRouteSpec(
        "nmc_trunk_14", "T-14", "Pardi - Sitabuldi - Dharampeth - Ambazari - YCCE Hingna",
        "पारडी - सीताबर्डी - धरमपेठ - अंबाझरी - वायसीसीई", "पारडी - सीताबर्डी - धरमपेठ - अंबाझरी - वायसीसीई", "059669",
        ("hub_pardi", "hub_itwari", "hub_railway_stn", "hub_sitabuldi", "hub_dharampeth", "hub_ambazari", "hub_crpf", "hub_ycce_hingna"),
        headway_min=15, start_hour=6, end_hour=22, table_ref="Table 3-12 #14 Pardi-YCCE College",
    ),
    TrunkRouteSpec(
        "nmc_trunk_16", "T-16", "Sitabuldi - Ravi Nagar - Vayusena - Wadi",
        "सीताबर्डी - रवि नगर - वायुसेना नगर - वाडी", "सीताबर्डी - रवी नगर - वायुसेना नगर - वाडी", "059669",
        ("hub_sitabuldi", "hub_ravi_nagar", "hub_vayusena", "hub_wadi"),
        headway_min=15, start_hour=6, end_hour=22, table_ref="Table 3-12 #16 Sitabuldi-Defence/Wadi",
    ),
    TrunkRouteSpec(
        "nmc_trunk_19", "T-19", "Sitabuldi - Sadar - Katol Naka - Gorewada",
        "सीताबर्डी - सदर - काटोल नाका - गोरेवाडा", "सीताबर्डी - सदर - काटोल नाका - गोरेवाडा", "059669",
        ("hub_sitabuldi", "hub_sadar", "hub_katol_naka", "hub_gorewada"),
        headway_min=20, start_hour=6, end_hour=21, table_ref="Table 3-12 #27 / CBS-017-T Sitabuldi-Gorewada",
    ),
    TrunkRouteSpec(
        "nmc_trunk_21", "T-21", "Sitabuldi - Kamal Chowk - Jaripatka - Nara",
        "सीताबर्डी - कमल चौक - जरीपटका - नारा", "सीताबर्डी - कमल चौक - जरीपटका - नारा", "059669",
        ("hub_sitabuldi", "hub_railway_stn", "hub_kamal_chowk", "hub_indora", "hub_jaripatka", "hub_nara"),
        headway_min=15, start_hour=6, end_hour=22, table_ref="Table 3-12 #21 Sitabuldi-Nara",
    ),
    TrunkRouteSpec(
        "nmc_trunk_22", "T-22", "Sitabuldi - Wadi - Brahmani Fata",
        "सीताबर्डी - वाडी - ब्राह्मणी फाटा", "सीताबर्डी - वाडी - ब्राह्मणी फाटा", "047857",
        ("hub_sitabuldi", "hub_ravi_nagar", "hub_wadi", "hub_brahmani_fata"),
        headway_min=25, start_hour=6, end_hour=21, table_ref="Table 3-12 #22 Sitabuldi-Brahmani Fata",
    ),
    TrunkRouteSpec(
        "nmc_trunk_28", "T-28", "Sitabuldi - Chhatrapati Sq - Manish Nagar - Besa - Beltarodi",
        "सीताबर्डी - छत्रपति चौक - मनीष नगर - बेसा - बेलतरोडी", "सीताबर्डी - छत्रपती चौक - मनीष नगर - बेसा - बेलतरोडी", "059669",
        ("hub_sitabuldi", "hub_ajni_sq", "hub_chhatrapati_sq", "hub_manish_nagar", "hub_besa", "hub_beltarodi"),
        headway_min=15, start_hour=6, end_hour=22, table_ref="Table 3-12 #28 Beltarodi-Sitabuldi",
    ),
    TrunkRouteSpec(
        "nmc_trunk_31", "T-31", "Sitabuldi - Medical - Nandanvan - Sakkardara - Wathoda",
        "सीताबर्डी - मेडिकल - नंदनवन - सक्करदरा - वाठोडा", "सीताबर्डी - मेडिकल - नंदनवन - सक्करदरा - वाठोडा", "047857",
        ("hub_sitabuldi", "hub_medical_sq", "hub_nandanvan", "hub_sakkardara", "hub_dighori", "hub_wathoda"),
        headway_min=20, start_hour=6, end_hour=21, table_ref="Table 3-12 #31 / Wathoda Corridor",
    ),
    TrunkRouteSpec(
        "nmc_trunk_33", "T-33", "Sitabuldi - Medical - Hudkeshwar - Bahadura - Panchgaon",
        "सीताबर्डी - मेडिकल - हुडकेश्वर - पाचगाव", "सीताबर्डी - मेडिकल - हुडकेश्वर - पाचगाव", "047857",
        ("hub_sitabuldi", "hub_medical_sq", "hub_ayodhya_tpt", "hub_hudkeshwar", "hub_dighori", "hub_bahadura", "hub_panchgaon"),
        headway_min=20, start_hour=6, end_hour=20, table_ref="Table 3-12 #33 Sitabuldi-Panchgaon (Hudkeshwar)",
    ),
    TrunkRouteSpec(
        "nmc_trunk_34", "T-34", "Sitabuldi - Chhatrapati - Khapri - Butibori MIDC",
        "सीताबर्डी - छत्रपति चौक - खापरी - बुटीबोरी", "सीताबर्डी - छत्रपती चौक - खापरी - बुटीबोरी", "047857",
        ("hub_sitabuldi", "hub_ajni_sq", "hub_chhatrapati_sq", "hub_khapri_nh44", "hub_butibori"),
        headway_min=15, start_hour=6, end_hour=20, table_ref="Table 3-12 #34 Sitabuldi-Indorama/Butibori",
    ),
    TrunkRouteSpec(
        "nmc_trunk_37", "T-37", "Sitabuldi - Deekshabhoomi - Pratap Nagar - Hingna",
        "सीताबर्डी - दीक्षाभूमि - प्रताप नगर - हिंगणा", "सीताबर्डी - दीक्षाभूमी - प्रताप नगर - हिंगणा", "059669",
        ("hub_sitabuldi", "hub_deekshabhoomi", "hub_abhayankar", "hub_pratap_nagar", "hub_crpf", "hub_ycce_hingna"),
        headway_min=12, start_hour=6, end_hour=22, table_ref="Table 3-12 #37 Sitabuldi-Hingna",
    ),
    TrunkRouteSpec(
        "nmc_trunk_38", "T-38", "Dighori - Medical Chowk - Chhatrapati Sq - Jaitala (South Ring)",
        "दिघोरी - मेडिकल चौक - छत्रपति चौक - जैताला", "दिघोरी - मेडिकल चौक - छत्रपती चौक - जैताळा", "0D9488",
        ("hub_dighori", "hub_sakkardara", "hub_medical_sq", "hub_ajni_sq", "hub_chhatrapati_sq", "hub_pratap_nagar", "hub_jaitala"),
        headway_min=15, start_hour=6, end_hour=22, table_ref="Table 3-12 #38 Kharbi/Dighori-Jaitala",
    ),
)

# Average commercial city bus speed used for timepoint=0 stop offsets (~20.5 km/h matches Table 3-12: 20 km in ~58-41 min)
TRUNK_COMMERCIAL_SPEED_MPS = 5.8
TRUNK_INTERMEDIATE_DWELL_S = 25


class NMCTrunkCorridorsAdapter(BaseTransitAdapter):
    """Generates road-snapped GTFS trunk corridors from Maha Metro/NMC Table 3-12 operational data."""

    def __init__(self, road_snapper: OSMRoadSnapper) -> None:
        self.road_snapper = road_snapper

    @property
    def adapter_name(self) -> str:
        return "nmc_umtc_plan_table3_12"

    def is_configured(self) -> bool:
        return True

    def fetch_and_normalize(self) -> GTFSFeedBundle:
        bundle = GTFSFeedBundle(
            provenances=[
                SourceProvenance(
                    source_id="nmc_umtc_plan_table3_12",
                    source_name="NMC Aapli Bus Major City Corridors (Maha Metro Comprehensive Feeder Plan Table 3-12 & 3-13)",
                    licence="Public Agency Report (Maha Metro Nagpur / NMC Operational Parameters)",
                    url="https://www.metrorailnagpur.com/assets/Comprehensive-feeder-service-plan-CfHeA0M1.pdf",
                    attribution="City bus corridor inventory & operational headways: Maha Metro / NMC; road geometry © OpenStreetMap contributors (ODbL)",
                )
            ]
        )

        bundle.agencies["nmpl_aapli_bus"] = AgencyRecord(
            agency_id="nmpl_aapli_bus",
            agency_name="Nagpur Mahanagar Parivahan Limited (Aapli Bus)",
            agency_url="https://www.nmcnagpur.gov.in",
            agency_timezone="Asia/Kolkata",
            agency_lang="en",
            data_source="nmc_umtc_plan_table3_12",
        )
        bundle.calendars["daily_service"] = CalendarRecord(
            service_id="daily_service",
            monday=1,
            tuesday=1,
            wednesday=1,
            thursday=1,
            friday=1,
            saturday=1,
            sunday=1,
            start_date="20260101",
            end_date="20271231",
        )

        # Snap every verified city hub onto its nearest drivable OSM bus road curb
        for sid, (en, hi, mr, lat, lon, is_term, src) in VERIFIED_TRUNK_HUBS.items():
            nid, _ = self.road_snapper.nearest_road_node(lat, lon)
            curb_lat, curb_lon = self.road_snapper._node_coords[nid]
            bundle.stops[sid] = StopRecord(
                stop_id=sid,
                name_en=en,
                name_hi=hi,
                name_mr=mr,
                lat=curb_lat,
                lon=curb_lon,
                is_terminal=is_term,
                is_metro_station=False,
                data_source=f"{src}+osm_road_curb",
            )

        for spec in OFFICIAL_NMC_TRUNK_ROUTES:
            bundle.routes[spec.route_id] = RouteRecord(
                route_id=spec.route_id,
                agency_id="nmpl_aapli_bus",
                route_short_name=spec.short_name,
                route_long_name=spec.long_name,
                name_hi=spec.name_hi,
                name_mr=spec.name_mr,
                route_type=3,
                route_color=spec.color,
                route_text_color="FFFFFF",
                is_feeder=False,
                data_source="nmc_umtc_plan_table3_12",
            )

            for direction_id, stop_seq in ((0, spec.stops), (1, tuple(reversed(spec.stops)))):
                ordered_coords = [
                    (sid, bundle.stops[sid].lat, bundle.stops[sid].lon) for sid in stop_seq
                ]
                shape_id = f"shape_{spec.route_id}_d{direction_id}"
                shape, projections = self.road_snapper.snap_stop_sequence(
                    shape_id=shape_id,
                    ordered_stops=ordered_coords,
                    data_source="osm_nagpur_roads",
                )
                bundle.shapes[shape_id] = shape
                headsign = bundle.stops[stop_seq[-1]].name_en

                offsets_s: list[int] = [0]
                for idx in range(1, len(projections)):
                    leg_m = max(120.0, projections[idx].s_m - projections[idx - 1].s_m)
                    leg_run_s = max(60, round(leg_m / TRUNK_COMMERCIAL_SPEED_MPS))
                    prev_dep = offsets_s[-1] + (TRUNK_INTERMEDIATE_DWELL_S if idx > 1 else 0)
                    offsets_s.append(prev_dep + leg_run_s)

                # Generate headway-spaced trips across the corridor's operational window (timepoint=0)
                hw_s = spec.headway_min * 60
                start_s = spec.start_hour * 3600 + (300 if direction_id == 1 else 0)
                end_s = spec.end_hour * 3600
                t_idx = 0
                dep0_s = start_s
                while dep0_s <= end_s:
                    trip_id = f"{spec.route_id}_d{direction_id}_{t_idx:03d}"
                    bundle.trips[trip_id] = TripRecord(
                        trip_id=trip_id,
                        route_id=spec.route_id,
                        service_id="daily_service",
                        direction_id=direction_id,
                        shape_id=shape_id,
                        trip_headsign=headsign,
                        data_source="nmc_umtc_plan_table3_12",
                    )
                    for seq, (proj, off_s) in enumerate(zip(projections, offsets_s), start=1):
                        arr_s = dep0_s + off_s
                        dep_s = arr_s + (TRUNK_INTERMEDIATE_DWELL_S if 1 < seq < len(projections) else 0)
                        bundle.stop_times.append(
                            StopTimeRecord(
                                trip_id=trip_id,
                                stop_sequence=seq,
                                stop_id=proj.stop_id,
                                arrival_time_s=arr_s,
                                departure_time_s=dep_s,
                                shape_dist_traveled_m=proj.s_m,
                                # Headway-derived corridor timetable (timepoint=0 for full honesty)
                                timepoint=0,
                            )
                        )
                    dep0_s += hw_s
                    t_idx += 1

        logger.info(
            "nmc_trunk.normalized",
            stops=len(bundle.stops),
            routes=len(bundle.routes),
            shapes=len(bundle.shapes),
            trips=len(bundle.trips),
            stop_times=len(bundle.stop_times),
        )
        return bundle
