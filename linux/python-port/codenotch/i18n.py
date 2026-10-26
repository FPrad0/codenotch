"""Language and clock, resolved from the Linux locale the way i18n.rs reads the Windows one, plus
the few strings the host itself draws (tray and right-click menus)."""
from __future__ import annotations

import locale
import os

LANGS = ("en", "zh", "zh-Hant", "ja", "ko", "pt-BR", "ru", "uk")


def language_from_locale(name: str):
    n = (name or "").lower().replace("_", "-")
    if n.startswith(("zh-tw", "zh-hant", "zh-hk", "zh-mo")):
        return "zh-Hant"
    for prefix, lang in (("zh", "zh"), ("ja", "ja"), ("ko", "ko"), ("ru", "ru"), ("uk", "uk"),
                         ("pt", "pt-BR"), ("en", "en")):
        if n.startswith(prefix):
            return lang
    return None


def resolve_auto() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var)
        if v and v not in ("C", "POSIX") and not v.startswith("C."):
            lang = language_from_locale(v.split(".")[0])
            if lang:
                return lang
    for part in (os.environ.get("LANGUAGE") or "").split(":"):
        lang = language_from_locale(part)
        if lang:
            return lang
    return "en"


def resolved(raw: str) -> str:
    return resolve_auto() if raw == "auto" else (raw if raw in LANGS else "en")


def clock_24h() -> bool:
    try:
        locale.setlocale(locale.LC_TIME, "")
        fmt = locale.nl_langinfo(locale.T_FMT)
    except (locale.Error, AttributeError, ValueError):
        return True
    return not any(tok in fmt for tok in ("%I", "%l", "%p", "%r"))


_T = {
    "refresh_all": {"en": "Refresh all", "pt-BR": "Atualizar tudo", "zh": "全部刷新", "zh-Hant": "全部重新整理",
                    "ja": "すべて更新", "ko": "모두 새로 고침", "ru": "Обновить всё", "uk": "Оновити все"},
    "settings": {"en": "Settings…", "pt-BR": "Ajustes…", "zh": "设置…", "zh-Hant": "設定…", "ja": "設定…",
                 "ko": "설정…", "ru": "Настройки…", "uk": "Налаштування…"},
    "quit_app": {"en": "Quit Codenotch", "pt-BR": "Encerrar o Codenotch", "zh": "退出 Codenotch",
                 "zh-Hant": "結束 Codenotch", "ja": "Codenotch を終了", "ko": "Codenotch 종료",
                 "ru": "Выйти из Codenotch", "uk": "Вийти з Codenotch"},
    "refresh_now": {"en": "Refresh now", "pt-BR": "Atualizar agora", "zh": "立即刷新", "zh-Hant": "立即重新整理",
                    "ja": "今すぐ更新", "ko": "지금 새로 고침", "ru": "Обновить сейчас", "uk": "Оновити зараз"},
    "open_host": {"en": "Open %@", "pt-BR": "Abrir o %@", "zh": "打开 %@", "zh-Hant": "開啟 %@", "ja": "%@ を開く",
                  "ko": "%@ 열기", "ru": "Открыть %@", "uk": "Відкрити %@"},
    "keep_open": {"en": "Keep open", "pt-BR": "Manter aberto", "zh": "保持展开", "zh-Hant": "保持展開",
                  "ja": "開いたままにする", "ko": "열어 두기", "ru": "Оставить открытым", "uk": "Тримати відкритим"},
    "waiting": {"en": "Waiting for the first reading…", "pt-BR": "Aguardando a primeira leitura…",
                "zh": "正在等待首次读数…", "zh-Hant": "正在等待首次讀數…", "ja": "最初の読み取りを待っています…",
                "ko": "첫 측정값을 기다리는 중…", "ru": "Ожидание первых данных…", "uk": "Очікування першого показника…"},
    "sign_in": {"en": "Sign in required", "pt-BR": "É preciso entrar", "zh": "需要登录", "zh-Hant": "需要登入",
                "ja": "サインインが必要です", "ko": "로그인 필요", "ru": "Требуется вход", "uk": "Потрібен вхід"},
    "stale": {"en": "stale", "pt-BR": "desatualizado", "zh": "已过时", "zh-Hant": "已過時", "ja": "古いデータ",
              "ko": "오래됨", "ru": "устарело", "uk": "застаріло"},
    "resets_in_min": {"en": "resets in %d min", "pt-BR": "redefine em %d min", "zh": "%d 分钟后重置",
                      "zh-Hant": "%d 分鐘後重置", "ja": "%d 分後にリセット", "ko": "%d분 후 재설정",
                      "ru": "сброс через %d мин", "uk": "скидання через %d хв"},
    "resets_in_h": {"en": "resets in %d h", "pt-BR": "redefine em %d h", "zh": "%d 小时后重置",
                    "zh-Hant": "%d 小時後重置", "ja": "%d 時間後にリセット", "ko": "%d시간 후 재설정",
                    "ru": "сброс через %d ч", "uk": "скидання через %d год"},
    "resets_in_d": {"en": "resets in %d d", "pt-BR": "redefine em %d d", "zh": "%d 天后重置",
                    "zh-Hant": "%d 天後重置", "ja": "%d 日後にリセット", "ko": "%d일 후 재설정",
                    "ru": "сброс через %d дн", "uk": "скидання через %d дн"},
}


def tr(lang: str, key: str) -> str:
    table = _T.get(key, {})
    return table.get(resolved(lang), table.get("en", key))
