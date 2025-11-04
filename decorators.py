import functools
from config import ADMIN_IDS

def _extract_user_from_args(args, kwargs):
    for v in list(args) + list(kwargs.values()):
        try:
            if hasattr(v, "from_user") and getattr(v, "from_user") is not None:
                return v
        except Exception:
            continue
    return None

def admin_only(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        obj = _extract_user_from_args(args, kwargs)
        user_id = None
        if obj is not None and hasattr(obj, "from_user"):
            user_id = getattr(obj.from_user, "id", None)

        if user_id not in ADMIN_IDS:
            try:
                if hasattr(obj, "answer") and callable(obj.answer):
                    await obj.answer("Доступ запрещён. Только администраторы.", show_alert=True)
                    return
            except Exception:
                pass
            try:
                if hasattr(obj, "reply") and callable(obj.reply):
                    await obj.reply("Доступ запрещён. Только администраторы.")
            except Exception:
                pass
            return
        return await func(*args, **kwargs)
    return wrapper
