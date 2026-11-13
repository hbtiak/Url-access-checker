"""
URL Availability Checker — runs 100% locally.

Mode A (VPN required = ON):
  Assumes the corporate VPN is connected. Internal URLs are checked from this
  machine over the VPN tunnel. VPN-specific settings (TLS bypass, custom auth
  headers) are exposed, and DNS/connect/timeout failures are flagged as likely
  VPN issues.

Mode B (VPN required = OFF):
  Checks public URL accessibility directly from this machine (which has normal
  internet access). Returns a straightforward per-URL success / failure report.

Run:
  pip install -r requirements.txt
  streamlit run app.py
  # open http://127.0.0.1:8501
"""

import asyncio
import json
import time
from typing import Optional

import httpx
import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------- #
# Page config
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="URL Availability Checker",
    page_icon="🔎",
    layout="wide",
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_urls(text: str) -> list[str]:
    """Accept a plain list (one URL per line) or a CSV column."""
    urls: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:                                    # CSV row → first URL cell
            for cell in line.split(","):
                cell = cell.strip().strip('"').strip("'")
                if cell.startswith(("http://", "https://")):
                    urls.append(cell)
                    break
        elif line.startswith(("http://", "https://")):
            urls.append(line)
    return urls


def classify_error(err_type: Optional[str], vpn_required: bool) -> str:
    """Turn a raw error type into a human-readable tag."""
    if err_type is None:
        return ""
    if vpn_required and err_type in ("connect_error", "dns_error", "timeout"):
        return f"{err_type} (likely VPN / DNS issue — confirm VPN is connected)"
    return err_type


# --------------------------------------------------------------------------- #
# Async checker
# --------------------------------------------------------------------------- #
async def check_one(client: httpx.AsyncClient, url: str, idx: int,
                    method: str, timeout: float) -> dict:
    start = time.perf_counter()
    try:
        if method.upper() == "HEAD":
            resp = await client.head(url, timeout=timeout)
            # Some servers reject HEAD but serve GET — retry so we don't false-fail.
            if resp.status_code in (403, 405, 501):
                resp = await client.get(url, timeout=timeout)
        else:
            resp = await client.get(url, timeout=timeout)

        latency = round((time.perf_counter() - start) * 1000, 1)
        return {
            "idx": idx,
            "url": url,
            "success": 200 <= resp.status_code < 400,
            "status": resp.status_code,
            "final_url": str(resp.url),
            "redirects": " -> ".join(str(h.url) for h in resp.history),
            "latency_ms": latency,
            "error_type": None,
            "error_detail": None,
            "body_snippet": (resp.text or "")[:200],
        }
    except httpx.TimeoutException as e:
        return _err(url, idx, start, "timeout", str(e))
    except httpx.ConnectError as e:
        msg = str(e)
        kind = "dns_error" if any(k in msg.lower() for k in ("name", "getaddrinfo", "nodename")) else "connect_error"
        return _err(url, idx, start, kind, msg)
    except httpx.TooManyRedirects as e:
        return _err(url, idx, start, "too_many_redirects", str(e))
    except httpx.HTTPError as e:
        return _err(url, idx, start, "http_error", str(e))
    except Exception as e:
        return _err(url, idx, start, "other", f"{type(e).__name__}: {e}")


def _err(url, idx, start, kind, detail):
    return {
        "idx": idx,
        "url": url,
        "success": False,
        "status": None,
        "final_url": None,
        "redirects": "",
        "latency_ms": round((time.perf_counter() - start) * 1000, 1),
        "error_type": kind,
        "error_detail": detail,
        "body_snippet": "",
    }


async def run_checks(urls, concurrency, timeout, method, headers, verify_tls, progress_cb):
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(
        max_connections=concurrency * 2,
        max_keepalive_connections=concurrency,
    )
    total = len(urls)
    results = []

    async with httpx.AsyncClient(
        limits=limits,
        headers=headers,
        verify=verify_tls,
        follow_redirects=True,
    ) as client:
        async def worker(i, u):
            async with sem:
                return await check_one(client, u, i, method, timeout)

        tasks = [asyncio.create_task(worker(i, u)) for i, u in enumerate(urls)]
        for fut in asyncio.as_completed(tasks):
            results.append(await fut)
            progress_cb(len(results), total)

    results.sort(key=lambda r: r["idx"])
    return results


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
st.title("🔎 URL Availability Checker")
st.caption(
    "Runs entirely on this machine. **VPN required** = the app expects the "
    "corporate VPN to be connected and treats DNS / connect failures as likely "
    "VPN issues. **VPN not required** = public URL accessibility is checked "
    "directly from this machine."
)

# --- Mode switch -----------------------------------------------------------
vpn_required = st.checkbox(
    "VPN required for these URLs",
    value=False,
    help="ON  → use the local VPN tunnel (internal URLs). "
         "OFF → check public URL accessibility from this machine.",
)

# --- Inputs ----------------------------------------------------------------
col_input, col_settings = st.columns([3, 2])

with col_input:
    urls_text = st.text_area(
        "Paste URLs (one per line, or a CSV column)",
        height=220,
        placeholder=(
            "https://proshop-jsp.dormakaba.com/de-de/1001_XX/configure/"
            "external/product/000009900057003409?clear=true"
        ),
    )
    uploaded = st.file_uploader("…or upload a .txt / .csv file", type=["txt", "csv"])

with col_settings:
    st.subheader("Check settings")
    concurrency = st.slider("Concurrency", 1, 200, 50,
                            help="Simultaneous in-flight requests.")
    timeout = st.number_input("Timeout (seconds)", 1.0, 120.0, 15.0, 1.0)
    method = st.selectbox(
        "HTTP method", ["HEAD", "GET"], 0,
        help="HEAD is lighter; app auto-retries with GET if HEAD is blocked.",
    )

    if vpn_required:
        verify_tls = st.checkbox(
            "Verify TLS certificate", value=True,
            help="Uncheck only if a corporate proxy re-signs TLS certificates.",
        )
        headers_input = st.text_area(
            "Extra headers (JSON, optional)",
            placeholder='{"Cookie": "SESSION=..."}',
            help="For endpoints that need authentication beyond the VPN tunnel.",
        )
    else:
        verify_tls = True
        headers_input = ""

# --- Parse inputs ----------------------------------------------------------
raw = urls_text or ""
if uploaded is not None:
    raw += "\n" + uploaded.read().decode("utf-8", errors="ignore")
urls = parse_urls(raw)

headers: dict = {}
if headers_input.strip():
    try:
        headers = json.loads(headers_input)
    except Exception as e:
        st.warning(f"Could not parse extra headers JSON: {e}")

mode_label = "VPN (local tunnel)" if vpn_required else "Public (direct)"

if urls:
    st.info(f"{len(urls)} URLs parsed · Mode: **{mode_label}**")
else:
    st.warning("Paste or upload at least one URL.")

# --- Run -------------------------------------------------------------------
run_clicked = st.button("Run checks", type="primary", disabled=not urls)

if run_clicked:
    progress_bar = st.progress(0.0)
    status_line = st.empty()

    def on_progress(done: int, total: int):
        progress_bar.progress(done / total)
        status_line.text(f"Checked {done}/{total}")

    with st.spinner("Running checks…"):
        results = asyncio.run(
            run_checks(urls, concurrency, timeout, method, headers,
                       verify_tls, on_progress)
        )

    progress_bar.progress(1.0)
    status_line.text(f"Done — {len(results)} URLs checked")

    # --- Report ------------------------------------------------------------
    st.subheader("Report")
    total = len(results)
    ok = sum(1 for r in results if r["success"])
    failed = total - ok

    c1, c2, c3 = st.columns(3)
    c1.metric("Total", total)
    c2.metric("✅ Success", ok)
    c3.metric("❌ Failure", failed)

    df = pd.DataFrame([{
        "URL": r["url"],
        "Result": "✅ SUCCESS" if r["success"] else "❌ FAILURE",
        "HTTP status": r["status"],
        "Latency (ms)": r["latency_ms"],
        "Error": classify_error(r["error_type"], vpn_required),
        "Error detail": r["error_detail"],
        "Final URL": r["final_url"],
        "Redirects": r["redirects"],
        "Body snippet": r["body_snippet"],
        "_success": r["success"],
    } for r in results])

    view = st.radio("Filter", ["All", "Success only", "Failure only"], horizontal=True)
    if view == "Success only":
        df_view = df[df["_success"]]
    elif view == "Failure only":
        df_view = df[~df["_success"]]
    else:
        df_view = df

    st.dataframe(
        df_view.drop(columns=["_success"]),
        use_container_width=True,
        hide_index=True,
    )

    st.download_button(
        "⬇️ Download full report (CSV)",
        data=df.drop(columns=["_success"]).to_csv(index=False).encode("utf-8"),
        file_name="url_report.csv",
        mime="text/csv",
    )

    # Mode-aware interpretation hint
    if vpn_required and failed > 0:
        vpn_errs = sum(
            1 for r in results
            if not r["success"] and r["error_type"] in ("connect_error", "dns_error", "timeout")
        )
        if vpn_errs:
            st.warning(
                f"{vpn_errs} failure(s) look like network-layer errors "
                "(DNS / connect / timeout). Confirm the VPN is connected and that "
                "the internal hostname resolves on this machine "
                "(try `nslookup proshop-jsp.dormakaba.com` in a terminal)."
            )
    if not vpn_required and failed > 0:
        st.info(
            "Failures in Public mode are genuine public-access problems "
            "(4xx/5xx, DNS, or timeout from this machine's internet connection)."
        )
