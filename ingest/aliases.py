"""Multilingual (English, Hindi, Marathi) stop aliasing & phonetic search normalizer for Nagpur."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from unidecode import unidecode

from ingest.models import StopRecord


@dataclass(frozen=True)
class NagpurStopTranslation:
    """Verified Hindi/Marathi names and local landmark aliases for major Nagpur hubs."""

    canonical_key: str
    name_hi: str
    name_mr: str
    aliases: tuple[str, ...]


# Curated Nagpur transit landmarks & bilingual spellings (English, Hindi, Marathi, Romanized variants)
NAGPUR_STOP_TRANSLATIONS: tuple[NagpurStopTranslation, ...] = (
    NagpurStopTranslation(
        "sitabuldi",
        "सीताबर्डी",
        "सीताबर्डी",
        ("Buldi", "Burdi", "Sitabuldi Interchange", "बर्डी", "Aapli Bus Terminal Sitabuldi", "Mor Bhavan"),
    ),
    NagpurStopTranslation(
        "khapri",
        "खापरी मेट्रो स्टेशन",
        "खापरी मेट्रो स्थानक",
        ("Khapri", "खापरी", "Khapri Metro", "MIHAN Feeder Hub"),
    ),
    NagpurStopTranslation(
        "aiims",
        "एम्स हॉस्पिटल नागपुर",
        "एम्स रुग्णालय नागपूर",
        ("AIIMS", "AIIMS Nagpur", "एम्स", "MIHAN AIIMS"),
    ),
    NagpurStopTranslation(
        "mihan",
        "मिहान",
        "मिहान",
        ("MIHAN", "MIHAN SEZ", "Mihan Square", "Infosys MIHAN", "TCS MIHAN"),
    ),
    NagpurStopTranslation(
        "sadar",
        "सदर",
        "सदर",
        ("Sadar Bazar", "सदर बाजार"),
    ),
    NagpurStopTranslation(
        "zero mile",
        "जीरो माइल फ्रीडम पार्क",
        "झिरो माईल फ्रीडम पार्क",
        ("Zero Mile", "झिरो माईल", "Freedom Park", "Central India Zero Mile"),
    ),
    NagpurStopTranslation(
        "automotive",
        "ऑटोमोटिव चौक",
        "ऑटोमोटिव्ह चौक",
        ("Automotive Square", "Kamptee Road Terminal", "ऑटोमोटिव्ह स्क्वेअर"),
    ),
    NagpurStopTranslation(
        "prajapati",
        "प्रजापति नगर",
        "प्रजापती नगर",
        ("Prajapati Nagar", "Pardi", "पारडी", "प्रजापती नगर मेट्रो"),
    ),
    NagpurStopTranslation(
        "lokmanya",
        "लोकमान्य नगर",
        "लोकमान्य नगर",
        ("Lokmanya Nagar", "Hingna", "हिंगणा", "लोकमान्य नगर मेट्रो"),
    ),
    NagpurStopTranslation(
        "bansi nagar",
        "बंसी नगर",
        "बंसी नगर",
        ("Bansi Nagar", "Hingna Road", "बंसी नगर मेट्रो"),
    ),
    NagpurStopTranslation(
        "airport",
        "नागपुर एयरपोर्ट",
        "नागपूर विमानतळ",
        ("Airport", "Dr Babasaheb Ambedkar Airport", "Sonegaon Airport", "विमानतळ", "एयरपोर्ट"),
    ),
    NagpurStopTranslation(
        "airport south",
        "एयरपोर्ट साउथ",
        "एअरपोर्ट साऊथ",
        ("Airport South Metro", "Chinchbhawan", "चिंचभवन"),
    ),
    NagpurStopTranslation(
        "new airport",
        "न्यू एयरपोर्ट",
        "न्यू एअरपोर्ट",
        ("New Airport Metro", "MIHAN North"),
    ),
    NagpurStopTranslation(
        "chhatrapati",
        "छत्रपति चौक",
        "छत्रपती चौक",
        ("Chhatrapati Square", "Wardha Road", "छत्रपती स्क्वेअर"),
    ),
    NagpurStopTranslation(
        "jaiprakash",
        "जयप्रकाश नगर",
        "जयप्रकाश नगर",
        ("JP Nagar", "Jayprakash Nagar", "जयप्रकाश नगर मेट्रो"),
    ),
    NagpurStopTranslation(
        "ujjwal",
        "उज्ज्वल नगर",
        "उज्ज्वल नगर",
        ("Ujjwal Nagar", "Somalwada", "सोमलवाडा"),
    ),
    NagpurStopTranslation(
        "ajni",
        "अजनी चौक",
        "अजनी चौक",
        ("Ajni Square", "अजनी", "Medical Square link"),
    ),
    NagpurStopTranslation(
        "rahate",
        "रहाटे कॉलोनी",
        "रहाटे कॉलनी",
        ("Rahate Colony", "Deekshabhoomi link", "दीक्षाभूमी"),
    ),
    NagpurStopTranslation(
        "congress nagar",
        "कांग्रेस नगर",
        "काँग्रेस नगर",
        ("Congress Nagar", "Dhantoli", "धंतोली", "Ajni Railway"),
    ),
    NagpurStopTranslation(
        "kasturchand",
        "कस्तूरचंद पार्क",
        "कस्तुरचंद पार्क",
        ("KP Ground", "Kasturchand Park", "Civil Lines", "सिव्हिल लाईन्स"),
    ),
    NagpurStopTranslation(
        "gaddi godam",
        "गड्डी गोदाम चौक",
        "गड्डी गोदाम चौक",
        ("Gaddi Godam", "Sadar link", "सदर"),
    ),
    NagpurStopTranslation(
        "kadvi",
        "कडवी चौक",
        "कडवी चौक",
        ("Kadbi Chowk", "Kadvi Chowk", "कडबी चौक"),
    ),
    NagpurStopTranslation(
        "indora",
        "इंदोरा चौक",
        "इंदोरा चौक",
        ("Indora Square", "इंदोरा"),
    ),
    NagpurStopTranslation(
        "nari road",
        "नारी रोड",
        "नारी रोड",
        ("Nari Road", "Kamptee Road"),
    ),
    NagpurStopTranslation(
        "nagpur railway",
        "नागपुर रेलवे स्टेशन",
        "नागपूर रेल्वे स्थानक",
        ("Nagpur Junction", "NGP Station", "रेल्वे स्टेशन", "Santra Market"),
    ),
    NagpurStopTranslation(
        "cotton market",
        "कॉटन मार्केट",
        "कॉटन मार्केट",
        ("Cotton Market", "Ganeshpeth", "गणेशपेठ", "MSRTC Bus Stand"),
    ),
    NagpurStopTranslation(
        "jhansi rani",
        "झांसी रानी चौक",
        "झाशी राणी चौक",
        ("Jhansi Rani Square", "Variety Square", "व्हरायटी चौक"),
    ),
    NagpurStopTranslation(
        "subhash nagar",
        "सुभाष नगर",
        "सुभाष नगर",
        ("Subhash Nagar", "IT Park", "आयटी पार्क"),
    ),
    NagpurStopTranslation(
        "shankar nagar",
        "शंकर नगर चौक",
        "शंकर नगर चौक",
        ("Shanker Nagar", "Dharampeth", "धरमपेठ"),
    ),
    NagpurStopTranslation(
        "lad",
        "एलएडी चौक",
        "एलएडी चौक",
        ("LAD Square", "Dharampeth", "धरमपेठ"),
    ),
    NagpurStopTranslation(
        "ambazari",
        "अंबाझरी झील",
        "अंबाझरी तलाव",
        ("Ambazari Lake View", "VNIT", "अंबाझरी"),
    ),
    NagpurStopTranslation(
        "vasudev",
        "वासुदेव नगर",
        "वासुदेव नगर",
        ("Vasudev Nagar", "Hingna Road"),
    ),
    NagpurStopTranslation(
        "rachna",
        "रचना रिंग रोड",
        "रचना रिंग रोड",
        ("Rachna Ring Road", "Jaitala", "जैताळा"),
    ),
    NagpurStopTranslation(
        "kamptee",
        "कामठी बस स्टैंड",
        "कामठी बस स्थानक",
        ("Kamptee", "कामठी", "Dragon Palace", "ड्रॅगन पॅलेस"),
    ),
    NagpurStopTranslation(
        "ycce",
        "वायसीसीई कॉलेज",
        "वायसीसीई कॉलेज",
        ("YCCE", "Wanadongri", "वानाडोंगरी", "Hingna"),
    ),
    NagpurStopTranslation(
        "cummins",
        "कमिन्स कॉलेज",
        "कमिन्स अभियांत्रिकी महाविद्यालय",
        ("Cummins College", "Sukli", "Hingna"),
    ),
    NagpurStopTranslation(
        "symbiosis",
        "सिम्बायोसिस विश्वविद्यालय",
        "सिम्बायोसिस विद्यापीठ",
        ("Symbiosis Nagpur", "Wathoda", "वाठोडा", "Bhandewadi"),
    ),
    NagpurStopTranslation(
        "lihigaon",
        "लिहीगाव",
        "लिहीगाव",
        ("Lihigaon", "Lihigoan", "Bharatwada", "भारतवाडा"),
    ),
    NagpurStopTranslation(
        "pallotti",
        "सेंट विंसेंट पल्लोटी कॉलेज",
        "सेंट व्हिन्सेंट पल्लोटी कॉलेज",
        ("SVPCET", "Pallotti College", "Gavsi Manapur", "Wardha Road"),
    ),
    NagpurStopTranslation(
        "brahmani",
        "ब्राह्मणी फाटा",
        "ब्राह्मणी फाटा",
        ("Brahmani Fata", "Brahmni Fata", "Wadi", "वाडी"),
    ),
    NagpurStopTranslation(
        "tcs",
        "टीसीएस मिहान",
        "टीसीएस मिहान",
        ("TCS Gate 2", "TCS MIHAN", "Infosys", "Tech Mahindra", "SP Infocity"),
    ),
    NagpurStopTranslation(
        "dassault",
        "डसॉल्ट रिलायंस एयरोस्पेस मिहान",
        "डसॉल्ट रिलायन्स एरोस्पेस मिहान",
        ("DRAL", "Dassault Reliance", "Air India MRO", "MIHAN SEZ"),
    ),
    NagpurStopTranslation(
        "smart data",
        "स्मार्ट डेटा मिहान",
        "स्मार्ट डेटा मिहान",
        ("Smart Data", "Essem Compliance", "Diet Food MIHAN"),
    ),
    NagpurStopTranslation(
        "wathoda",
        "वाठोडा",
        "वाठोडा",
        ("Wathoda", "Wathoda Layout"),
    ),
    NagpurStopTranslation(
        "hudkeshwar",
        "हुडकेश्वर",
        "हुडकेश्वर",
        ("Hudkeshwar", "Hudkeshwar Road"),
    ),
    NagpurStopTranslation(
        "butibori",
        "बुटीबोरी",
        "बुटीबोरी",
        ("Butibori MIDC", "Butibori"),
    ),
)


def normalize_search_text(text: str) -> str:
    """Normalize multilingual EN/HI/MR query or stop name for fuzzy + transliterated search."""
    nfc = unicodedata.normalize("NFKC", text).strip().lower()
    # Preserve Devanagari tokens alongside Romanized transliteration for cross-script matching
    ascii_translit = unidecode(nfc).lower()
    # Fold common Indian English / Marathi transliteration variants (ee->i, oo->u, ph->f, chh->ch)
    phonetic = ascii_translit
    phonetic = re.sub(r"ee", "i", phonetic)
    phonetic = re.sub(r"oo", "u", phonetic)
    phonetic = re.sub(r"chh", "ch", phonetic)
    phonetic = re.sub(r"ph", "f", phonetic)
    phonetic = re.sub(r"[^a-z0-9\s]", " ", phonetic)
    phonetic = " ".join(phonetic.split())

    raw_clean = " ".join(re.sub(r"[^\w\s\u0900-\u097F]", " ", nfc).split())
    combined_parts = [raw_clean]
    if ascii_translit and ascii_translit != raw_clean:
        combined_parts.append(" ".join(re.sub(r"[^a-z0-9\s]", " ", ascii_translit).split()))
    if phonetic and phonetic not in combined_parts:
        combined_parts.append(phonetic)
    return " | ".join(p for p in combined_parts if p)


def enrich_stop_multilingual(stop: StopRecord) -> StopRecord:
    """Attach Hindi/Marathi names and Nagpur landmark aliases to a StopRecord."""
    lowered = stop.name_en.lower()
    name_hi = stop.name_hi
    name_mr = stop.name_mr
    aliases = list(stop.aliases)

    for tr in NAGPUR_STOP_TRANSLATIONS:
        if tr.canonical_key in lowered:
            if not name_hi:
                name_hi = tr.name_hi
            if not name_mr:
                name_mr = tr.name_mr
            for a in tr.aliases:
                if a not in aliases and a.lower() != lowered:
                    aliases.append(a)

    return StopRecord(
        stop_id=stop.stop_id,
        name_en=stop.name_en,
        lat=stop.lat,
        lon=stop.lon,
        stop_code=stop.stop_code,
        name_hi=name_hi,
        name_mr=name_mr,
        aliases=tuple(aliases),
        parent_station=stop.parent_station,
        location_type=stop.location_type,
        is_terminal=stop.is_terminal,
        is_metro_station=stop.is_metro_station,
        data_source=stop.data_source,
    )


def build_stop_search_blob(stop: StopRecord) -> str:
    """Build combined searchable string across EN, HI, MR, aliases, and phonetic transliteration."""
    pieces = [stop.name_en, stop.name_hi, stop.name_mr, *stop.aliases]
    return " | ".join(normalize_search_text(p) for p in pieces if p)
