import os
import re
import datetime as dt
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

BROADCAST_DISABLED_VALUES = {"", "off", "disable", "disabled", "0"}
BROADCAST_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
ACTIVE_PAYMENT_STATUSES = ("pending", "processing_paid", "invite_error", "delivery_error", "processing_delivery")
RETRYABLE_PAYMENT_STATUSES = ("pending", "invite_error", "delivery_error")
MIN_WITHDRAWAL_AMOUNT = 10_000
WIB = dt.timezone(dt.timedelta(hours=7))

FIRST_NAMES = [
    "Agus", "Andi", "Bambang", "Budi", "Dedi", "Dian", "Eka", "Fajar", "Hendra", "Joko",
    "Rizki", "Sari", "Siti", "Taufik", "Wahyu", "Aditya", "Aldy", "Ari", "Aulia",
    "Bagus", "Cahyo", "Dani", "Dika", "Doni", "Edi", "Edwin", "Farhan", "Guntur", "Galih",
    "Heri", "Ilham", "Indra", "Kevin", "Lukman", "Mulyadi", "Naufal", "Nugraha", "Putra",
    "Rahmat", "Ryan", "Sandi", "Teguh", "Tommy", "Udin", "Yanto", "Yusuf", "Zaki", "Amanda",
    "Annisa", "Bella", "Citra", "Dewi", "Fitri", "Indah", "Mega", "Nita", "Putri", "Rina",
    "Shinta", "Wulan", "Yeni", "Abimanyu", "Achmad", "Agung", "Ahmad", "Akbar", "Akhmad",
    "Alamsyah", "Ali", "Alif", "Arief", "Aris", "Arif", "Arya", "Asrul", "Azhar", "Bagas",
    "Basuki", "Bayu", "Cecep", "Candra", "Dadan", "Dandi", "Danu", "Darma", "Dedy", "Denny",
    "Dewa", "Doddy", "Dodi", "Eko", "Elang", "Endra", "Erik", "Erwan", "Fachri", "Fadil",
    "Fadlan", "Fadli", "Faisal", "Fanani", "Febri", "Febrian", "Firman", "Firmansyah", "Fuad",
    "Gading", "Gilang", "Ginanjar", "Gunadi", "Hady", "Hafiz", "Halim", "Hamzah", "Hardi",
    "Hari", "Hartono", "Hary", "Hasan", "Hidayatullah", "Ibrohim", "Ichsan", "Ihsan", "Imam",
    "Iqbal", "Irfan", "Iwan", "Jafar", "Jaka", "Jamal", "Junaedi", "Kamal", "Luthfi", "Mansur",
    "Maulana", "Misbah", "Muammar", "Muchammad", "Mudji", "Muhammad", "Mulyono", "Munir",
    "Nanda", "Nanang", "Nasir", "Norman", "Nur", "Nurhadi", "Panca", "Panji", "Permadi",
    "Prasetya", "Prasetyo", "Pratama", "Prayitno", "Purwanto", "Raden", "Raditya", "Rafli",
    "Rahman", "Rakhmat", "Ramdan", "Randi", "Rangga", "Rian", "Riki", "Riko", "Riza", "Rizal",
    "Robby", "Roni", "Rudi", "Ruslan", "Sahrul", "Salim", "Satria", "Satrio", "Setyo", "Sigit",
    "Slamet", "Sofyan", "Suhendra", "Sulaiman", "Surya", "Syahputra", "Syarif", "Tri", "Untung",
    "Wawan", "Wibowo", "Wicaksono", "Widi", "Wijaya", "Wildan", "Winarto", "Wisnu", "Yahya",
    "Yoga", "Yudi", "Yuli", "Yulian", "Yusrul", "Zainal", "Anggraeni", "Anggraini", "Anita",
    "Aprilia", "Ariani", "Arum", "Astuti", "Ayu", "Carissa", "Cut", "Diah", "Diana", "Dwi",
    "Dyah", "Elisa", "Erika", "Esti", "Febby", "Febriana", "Gita", "Hesti", "Intan", "Irma",
    "Kartika", "Kiki", "Kusuma", "Laras", "Larasati", "Lestari", "Lia", "Linda", "Listya",
    "Lusi", "Maharani", "Maria", "Maya", "Melati", "Melinda", "Mita", "Monica", "Nadia",
    "Nadya", "Natalia", "Nenden", "Nia", "Ningrum", "Novia", "Novita", "Nurul", "Paramita",
    "Poppy", "Puspita", "Rahma", "Rahmasari", "Rahmawati", "Rani", "Ratna", "Restu", "Retno",
    "Rini", "Ririn", "Riska", "Risma", "Rosita", "Safitri", "Sandra", "Santi", "Septiana",
    "Sherly", "Silvia", "Siska", "Suci", "Sylvia", "Tantri", "Tika", "Triana", "Utami",
    "Valery", "Vina", "Wati", "Widya", "Wiwin", "Yanti", "Yulia", "Yuliana"
]

LAST_NAMES = [
    "Saputra", "Pratama", "Santoso", "Wijaya", "Nugroho", "Kurniawan", "Hidayat", "Setiawan",
    "Permana", "Ramadhan", "Maulana", "Lestari", "Aditama", "Anggara", "Budiman", "Cahyono",
    "Darmawan", "Gunawan", "Kusuma", "Wibowo", "Siregar", "Simanjuntak", "Nasution", "Harahap",
    "Pane", "Ginting", "Sinaga", "Sitorus", "Tanjung", "Lubis", "Gultom", "Batubara", "Pohan",
    "Rajagukguk", "Hutapea", "Manurung", "Hutabarat", "Pasaribu", "Nababan", "Sianipar",
    "Manik", "Panjaitan", "Fitriadi", "Haryanto", "Iskandar", "Jaya", "Kartika", "Mandala",
    "Nurdin", "Pamungkas", "Prayogo", "Raharjo", "Subagyo", "Suherman", "Susanto", "Widyawati",
    "Abidin", "Adnan", "Alamsyah", "Alatas", "Alhabsyi", "Alkatiri", "Amalia", "Anam", "Anggoro",
    "Anhar", "Antoro", "Aprianto", "Ardiansyah", "Arifin", "Aristya", "Assegaf", "Astaman",
    "Azhari", "Bachdim", "Baswedan", "Budianto", "Bunjamin", "Djojohadikusumo", "Effendi",
    "Fachir", "Fadilah", "Fauzi", "Febrian", "Ghazali", "Ghozali", "Hakim", "Hamzah", "Hanafi",
    "Hapsari", "Harsono", "Hartawan", "Hartono", "Hasibuan", "Heryanto", "Hidayatullah",
    "Husen", "Husin", "Ibrahim", "Idris", "Ihsan", "Indrawan", "Irwansyah", "Ismail", "Kalla",
    "Karim", "Kartasasmita", "Kiemas", "Kusumo", "Latif", "Latuconsina", "Lazuardi", "Lim",
    "Liyadi", "Mada", "Mangkuningrat", "Marbun", "Margono", "Masduki", "Mulyadi", "Mulyono",
    "Munandar", "Murtopo", "Mustofa", "Muttaqin", "Muzakki", "Natsir", "Nurhadi", "Pambudi",
    "Pangestu", "Parikesit", "Pinem", "Pradipta", "Pradana", "Prakoso", "Prasetyo", "Priyanto",
    "Purnomo", "Purwanto", "Qadri", "Raditya", "Rafli", "Rahardjo", "Rahman", "Rasyid",
    "Riswanto", "Riyadh", "Romadoni", "Rosadi", "Rusadi", "Sadikin", "Safei", "Sani", "Sasmita",
    "Sastro", "Sastrowardoyo", "Setyawan", "Shihab", "Sidabutar", "Soebandrio", "Soedarsono",
    "Soedjatmoko", "Soegondo", "Soeharto", "Soekarnoputri", "Soekarno", "Soemitro", "Soepomo",
    "Soeprapto", "Soeraji", "Soesilo", "Soetomo", "Sudarsono", "Sudirman", "Suhardi", "Suhartono",
    "Sukarno", "Sulistyo", "Sumantri", "Suparman", "Supriyadi", "Supriyanto", "Suroso",
    "Susilo", "Sutanto", "Sutrisno", "Syaifullah", "Syah", "Syahputra", "Syarif", "Syarifuddin",
    "Tan", "Tarigan", "Taufiq", "Tilaar", "Tobing", "Trianto", "Utomo", "Wahid", "Wardana",
    "Wardhani", "Wibisono", "Wicaksono", "Widianto", "Widjaja", "Widodo", "Widyatmoko",
    "Winata", "Wirawan", "Yulianto", "Yunus", "Zulkarnain"
]

@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    vip_chat_id: int
    log_chat_id: int
    sociabuzz_username: str
    sociabuzz_cookie: str
    payment_amount: int
    invite_expire_hours: int
    poll_interval_seconds: int
    poll_max_attempts: int
    poll_batch_size: int
    qris_create_concurrency: int
    broadcast_batch_size: int
    admin_user_ids: set[int]
    supabase_url: str
    supabase_service_role_key: str
    supabase_table: str
    supabase_package_table: str
    user_table: str
    referral_table: str
    withdrawal_table: str


def env_required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def env_int(name, default=None):
    value = os.getenv(name, "").strip()
    if not value:
        if default is None:
            raise RuntimeError(f"Missing required env: {name}")
        return default
    return int(value)


def env_optional_int(name, default=0):
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def parse_admin_ids(raw):
    ids = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            ids.add(int(item))
    return ids


def load_config():
    poll_batch_size = max(1, env_int("POLL_BATCH_SIZE", 20))
    return Config(
        api_id=env_int("TELEGRAM_API_ID"),
        api_hash=env_required("TELEGRAM_API_HASH"),
        bot_token=env_required("TELEGRAM_BOT_TOKEN"),
        vip_chat_id=env_optional_int("VIP_CHAT_ID"),
        log_chat_id=env_optional_int("LOG_CHAT_ID"),
        sociabuzz_username=env_required("SOCIABUZZ_USERNAME"),
        sociabuzz_cookie=os.getenv("SOCIABUZZ_COOKIE", "").strip(),
        payment_amount=env_int("PAYMENT_AMOUNT", 2000),
        invite_expire_hours=env_int("INVITE_EXPIRE_HOURS", 24),
        poll_interval_seconds=env_int("POLL_INTERVAL_SECONDS", 3),
        poll_max_attempts=env_int("POLL_MAX_ATTEMPTS", 300),
        poll_batch_size=poll_batch_size,
        qris_create_concurrency=max(1, env_int("QRIS_CREATE_CONCURRENCY", 5)),
        broadcast_batch_size=max(1, env_int("BROADCAST_BATCH_SIZE", poll_batch_size)),
        admin_user_ids=parse_admin_ids(os.getenv("ADMIN_USER_IDS", "")),
        supabase_url=env_required("SUPABASE_URL"),
        supabase_service_role_key=env_required("SUPABASE_SERVICE_ROLE_KEY"),
        supabase_table=os.getenv("SUPABASE_TABLE", "vip_payments").strip() or "vip_payments",
        supabase_package_table=os.getenv("SUPABASE_PACKAGE_TABLE", "vip_packages").strip() or "vip_packages",
        user_table=os.getenv("SUPABASE_USER_TABLE", "vip_users").strip() or "vip_users",
        referral_table=os.getenv("SUPABASE_REFERRAL_TABLE", "vip_referrals").strip() or "vip_referrals",
        withdrawal_table=os.getenv("SUPABASE_WITHDRAWAL_TABLE", "vip_withdrawals").strip() or "vip_withdrawals",
    )
