from core.registry import load_all, get
from core.base_platform import RegisterConfig

load_all()
cls = get("mixroute")
p = cls(RegisterConfig())
a1 = p.build_protocol_mailbox_adapter()
a2 = p.build_protocol_oauth_adapter()
a3 = p.build_browser_registration_adapter()
print("protocol_mailbox_adapter:", type(a1).__name__, "use_captcha=", a1.use_captcha)
print("protocol_oauth_adapter:", type(a2).__name__)
print("browser_registration_adapter:", type(a3).__name__, "use_captcha_for_mailbox=", a3.use_captcha_for_mailbox)

class A:
    token = ""
    extra = {}
print("check_valid(empty):", p.check_valid(A()))
