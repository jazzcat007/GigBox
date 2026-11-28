"""First-run gate for the hotspot: make the owner choose a passphrase before using the UI.

The hotspot may come up with the published default passphrase (policy ``gate``, the default), so a new
rig works out of the box. But the first thing a phone that joined it sees, on any page, is a form that
makes it choose a passphrase of its own. Until that is done, every request that arrives **on the
hotspot's own address** is answered with that form (pages) or a refusal (the API). Requests from the
Pi's own panel (loopback) and from the house network are never gated.

Why this is safe enough, and where it is not:

* The form is open to whoever reaches it while the passphrase is still the default. Setting it needs no
  write token, because a fresh owner has none. Anyone near who has read the docs could join in that
  window and set it first (the owner would then be locked out of the hotspot until they reset it at the
  Pi with ``fm1-switch net passphrase --prompt``). That is the price of a hotspot that works out of
  the box; the policy ``refuse`` removes it, and ``allow`` removes the gate.
* The passphrase is never logged, never returned, and never leaves this process except to NetworkManager.
* After it is set, the gate is gone and the normal rules (write token for off-Pi writes) apply.

Detecting "arrived on the hotspot": the server asks which address the connection came in on. When the
hotspot is up, that address is the one NetworkManager gave wlan0; if it cannot be read, an address in
NetworkManager's shared range (10.42.x.x) counts.
"""

from __future__ import annotations

import html
import ipaddress
import threading
import time
from typing import Callable

from . import config, netctl

POLICIES = ("gate", "refuse", "allow")
_CACHE: dict = {"at": -1e9, "ip": None}


def policy() -> str:
    return str(config.value("net_default_passphrase"))


def hotspot_ip(run: netctl.Runner = None, iface: str | None = None,
               clock: Callable[[], float] = time.monotonic) -> str | None:
    """The address the hotspot answers on while it is up (cached for 5 s), else None."""
    now = clock()
    if now - _CACHE["at"] < 5.0:
        return _CACHE["ip"]
    run = run or netctl.nmcli
    iface = iface or str(config.value("net_iface"))
    ip = None
    try:
        if netctl.wifi_state(run, iface).hotspot:
            ip = netctl.address(run, iface) or None
    except netctl.NetError:
        ip = None
    _CACHE["at"], _CACHE["ip"] = now, ip
    return ip


def _is_loopback(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def on_hotspot(local_ip: str, client_ip: str, ip_fn: Callable[[], str | None] = hotspot_ip) -> bool:
    """Did this request arrive over the hotspot (not from the panel, not from the house network)?"""
    if _is_loopback(client_ip) or not local_ip:
        return False
    ip = ip_fn()
    if ip:
        return local_ip == ip
    return local_ip.startswith("10.42.")        # NetworkManager's shared range, if we cannot ask


def gate_active(local_ip: str, client_ip: str, ip_fn: Callable[[], str | None] = hotspot_ip) -> bool:
    """Must this request be answered with the set-a-passphrase form?"""
    return (policy() == "gate" and netctl.is_default_passphrase()
            and on_hotspot(local_ip, client_ip, ip_fn))


class Refused(ValueError):
    """The passphrase offered cannot be used. The message says why."""


def check_new_passphrase(text: str) -> str:
    if not isinstance(text, str) or not 8 <= len(text) <= 63:
        raise Refused("a WiFi passphrase is 8 to 63 characters")
    if not text.isprintable() or "\n" in text:
        raise Refused("use printable characters only")
    if text == netctl.DEFAULT_PASSPHRASE:
        raise Refused("that is the published default: choose your own")
    return text


def apply_passphrase(new: str, run: netctl.Runner = None, *, restart_after: float = 2.5) -> dict:
    """Save the owner's passphrase and update the hotspot profile. The network restarts a moment
    later (so this answer can be delivered first); phones must rejoin with the new passphrase."""
    check_new_passphrase(new)
    run = run or netctl.nmcli
    netctl.write_passphrase(new)
    ssid = str(config.value("net_ssid"))
    if netctl.hotspot_exists(run):
        netctl.ensure_hotspot(run, iface=str(config.value("net_iface")), ssid=ssid,
                              band=str(config.value("net_band")),
                              channel=int(config.value("net_channel")), psk=new)
    if restart_after >= 0:
        threading.Timer(restart_after, _restart_hotspot, args=(run,)).start()
    _CACHE["at"] = -1e9
    return {"ok": True, "ssid": ssid, "restart_in": restart_after}


def _restart_hotspot(run: netctl.Runner) -> None:
    """Bring the hotspot down and up so it uses the new passphrase. Never raises."""
    try:
        if netctl.wifi_state(run, str(config.value("net_iface"))).hotspot:
            netctl.hotspot_down(run)
            netctl.hotspot_up(run)
    except Exception:  # noqa: BLE001 - nothing to report to: the caller has already answered
        pass


_STYLE = """
body{margin:0;background:#12161a;color:#e8ecef;font:17px/1.45 system-ui,sans-serif}
main{max-width:30rem;margin:0 auto;padding:1.5rem 1.25rem}
h1{font-size:1.4rem;margin:.2rem 0 1rem}
p{margin:.6rem 0}
.warn{background:#3a2a10;border:1px solid #8a6a1f;border-radius:10px;padding:.8rem 1rem}
label{display:block;margin:1rem 0 .3rem;font-weight:600}
input[type=password],input[type=text]{width:100%;box-sizing:border-box;padding:.7rem;border-radius:8px;
 border:1px solid #3a444d;background:#0c0f12;color:inherit;font:inherit}
button{margin-top:1.2rem;width:100%;padding:.85rem;border:0;border-radius:10px;background:#f0a000;
 color:#111;font:inherit;font-weight:700}
button:disabled{opacity:.5}
.err{color:#ff8b73;min-height:1.4em;margin-top:.6rem}
.ok{background:#12301f;border:1px solid #2c7a4a;border-radius:10px;padding:.8rem 1rem}
code{background:#0c0f12;padding:.1rem .35rem;border-radius:5px}
"""

_SCRIPT = """
const f=document.getElementById('f'),err=document.getElementById('err'),btn=document.getElementById('go');
document.getElementById('show').addEventListener('change',e=>{
  for(const id of ['p1','p2'])document.getElementById(id).type=e.target.checked?'text':'password';});
f.addEventListener('submit',async ev=>{
  ev.preventDefault();err.textContent='';
  const a=document.getElementById('p1').value,b=document.getElementById('p2').value;
  if(a!==b){err.textContent='The two entries are different.';return;}
  btn.disabled=true;
  try{
    const r=await fetch('/api/wifi/setup',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({passphrase:a})});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){err.textContent=d.error||('Refused ('+r.status+')');btn.disabled=false;return;}
    document.getElementById('main').innerHTML=
      '<h1>Saved</h1><div class="ok"><p>Your passphrase is set. The network <b>'+d.ssid+
      '</b> restarts in a few seconds, so this phone will disconnect.</p>'+
      '<p>Join <b>'+d.ssid+'</b> again with your <b>new</b> passphrase (forget the old entry first if the '+
      'phone keeps trying it), then open <code>http://fm-pi.local/</code>.</p></div>';
  }catch(e){
    // The network may already be restarting: that also means it worked.
    document.getElementById('main').innerHTML=
      '<h1>Probably saved</h1><div class="ok"><p>The connection dropped, which is what happens when the '+
      'network restarts with your new passphrase. Join the network again with the <b>new</b> passphrase, '+
      'then open <code>http://fm-pi.local/</code>. If it will not accept it, run '+
      '<code>fm1-switch net passphrase</code> on the Pi to see what is set.</p></div>';
  }
});
"""


def page(ssid: str | None = None, *, gated: bool = True, is_default: bool | None = None) -> bytes:
    """The set-a-passphrase page (``gated``), or a short note for someone who is not gated (the panel, the
    house network): either it is already set, or it is still the default and phones will be asked."""
    ssid = html.escape(ssid or str(config.value("net_ssid")))
    if is_default is None:
        is_default = netctl.is_default_passphrase()
    head = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Set the hotspot passphrase</title><style>{_STYLE}</style></head><body>")
    if not gated:
        if is_default:
            body = ('<main><h1>The hotspot still uses the default passphrase</h1>'
                    f'<p>The network <b>{ssid}</b> still uses the passphrase printed in the public docs. '
                    'The first phone that joins it is asked to choose one before it can use anything.</p>'
                    '<p>To choose it yourself, run <code>fm1-switch net passphrase --prompt</code> on the Pi.</p>'
                    '<p><a href="/" style="color:#f0a000">Open the fleet screen</a></p></main>')
        else:
            body = ('<main><h1>Passphrase already set</h1><p>The hotspot passphrase has been changed from '
                    'the default, so there is nothing to do here.</p><p>To change it later, run '
                    '<code>fm1-switch net passphrase --prompt</code> on the Pi.</p>'
                    '<p><a href="/" style="color:#f0a000">Open the fleet screen</a></p></main>')
        return (head + body + "</body></html>").encode()
    body = (
        '<main id="main"><h1>Choose your hotspot passphrase</h1>'
        f'<div class="warn"><p>This Pi\'s network <b>{ssid}</b> is still using the passphrase that is '
        'printed in the public docs, so anyone who has read them could join it.</p>'
        '<p>Choose your own now. You need it to join this network from now on.</p></div>'
        '<form id="f" autocomplete="off"><label for="p1">New passphrase (8 to 63 characters)</label>'
        '<input id="p1" type="password" minlength="8" maxlength="63" required autocomplete="new-password">'
        '<label for="p2">Again</label>'
        '<input id="p2" type="password" minlength="8" maxlength="63" required autocomplete="new-password">'
        '<p><label style="font-weight:400;margin:.6rem 0"><input id="show" type="checkbox"> '
        'Show what I type</label></p>'
        '<button id="go" type="submit">Save passphrase</button><div id="err" class="err"></div></form>'
        f"</main><script>{_SCRIPT}</script>")
    return (head + body + "</body></html>").encode()
