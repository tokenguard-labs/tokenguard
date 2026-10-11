"""LocalPilot demo UI. Start the API first, then:
    streamlit run scripts/streamlit_ui.py
"""
import requests
import streamlit as st

API = "http://localhost:8000"

st.set_page_config(page_title="LocalPilot", page_icon="🧭", layout="wide")

if "messages" not in st.session_state:
    st.session_state.messages = []   # {"role", "content", "meta"}
if "pending" not in st.session_state:
    st.session_state.pending = None  # request waiting for the user's local/global choice
if "toast" not in st.session_state:
    st.session_state.toast = None    # notification to show after the next rerun

if st.session_state.toast:
    st.toast(st.session_state.toast, icon="🖥️")
    st.session_state.toast = None


# ---------- API helpers ----------

def api_analyze(req):
    r = requests.post(f"{API}/analyze", json=req, timeout=60)
    r.raise_for_status()
    return r.json()


def api_chat(req, route):
    r = requests.post(f"{API}/chat", json={**req, "force_route": route}, timeout=600)
    if r.status_code != 200:
        raise RuntimeError(r.json().get("detail", r.text))
    return r.json()


def run_and_record(req, route, analysis):
    label = "local LLM" if route == "local" else "global (cloud) LLM"
    with st.spinner(f"Asking the {label}..."):
        try:
            result = api_chat(req, route)
        except Exception as e:
            st.session_state.messages.append({"role": "assistant", "content": f"⚠️ Error: {e}", "meta": None})
            return
    st.session_state.messages.append({
        "role": "assistant",
        "content": result["answer"],
        "meta": {**result, "request": req, "analysis": analysis},
    })


# ---------- Sidebar ----------

with st.sidebar:
    st.header("🧭 LocalPilot")
    try:
        health = requests.get(f"{API}/health", timeout=10).json()
        provider = health["cloud_provider"]
        if health.get("cloud_valid"):
            st.success(f"API online · cloud: `{provider}` ✓")
        elif provider == "mock":
            st.info("API online · cloud: `mock` (no real model)")
        else:
            st.warning(f"API online · cloud `{provider}` unusable: {health.get('cloud_detail')}")
        st.caption(f"Local model: `{health['local_model']}` · Ollama {'✅' if health['ollama'] else '❌'}")
    except Exception:
        st.error("API offline. Start it with:\n\n`make backend`")
        st.stop()

    st.subheader("Context (optional)")
    repo_path = st.text_input("Repo path", placeholder="/path/to/project")
    code = st.text_area("Selected code", height=150, placeholder="Paste code the prompt refers to")

    st.subheader("Session stats")
    try:
        s = requests.get(f"{API}/stats", timeout=3).json()
        c1, c2 = st.columns(2)
        c1.metric("Local", s["local_requests"])
        c2.metric("Cloud", s["cloud_requests"])
        st.metric("Cloud tokens saved", f"{s['total_tokens_saved']:,}", f"{s['cloud_token_reduction_pct']}%")
        if s.get("mock_requests"):
            st.caption(f"{s['mock_requests']} mock response(s) excluded from cloud metrics")
    except Exception:
        pass


# ---------- Chat history ----------

st.title("LocalPilot")
st.caption("Simple tasks run on your machine. Complex ones ask before going to the cloud.")


def route_summary(meta):
    """One line describing the routing decision, from the policy metadata."""
    parts = []
    if meta.get("complexity"):
        score = meta.get("complexity_score_100")
        parts.append(f"complexity {meta['complexity']}" + (f" ({score}/100)" if score is not None else ""))
    if meta.get("initial_route"):
        parts.append(f"initial {meta['initial_route']}")
    parts.append(f"final {meta.get('final_route') or meta['route']}")
    if meta.get("escalation_kind"):
        parts.append(f"escalation {meta['escalation_kind']}")
    return " · ".join(parts)


def render_meta(meta, idx):
    summary = route_summary(meta)
    if meta["route"] == "local":
        st.caption(f"🖥️ **Local LLM** · `{meta['model']}` · confidence {meta['confidence']} · "
                   f"{meta['latency_ms']} ms · 0 tokens sent to cloud")
        st.caption(f"Routing: {summary}")
        # Local answer looks shaky -> nudge harder; either way the user can opt in to the cloud
        low_conf = meta["confidence"] is not None and meta["confidence"] < meta["analysis"]["min_local_confidence"]
        if low_conf:
            st.warning("The local model wasn't confident about this answer.")
        label = "☁️ Retry with global LLM" if low_conf else "Not satisfied with this answer? ☁️ Use global LLM"
        if st.button(label, key=f"retry_{idx}", type="primary" if low_conf else "secondary"):
            run_and_record(meta["request"], "cloud", meta["analysis"])
            st.rerun()
    else:
        mock = meta.get("is_mock")
        st.caption(f"{'🧪' if mock else '☁️'} **{'Mock LLM' if mock else 'Global LLM'}** · `{meta['model']}` · "
                   f"{meta['latency_ms']} ms · sent {meta['sent_tokens']:,} of {meta['original_tokens']:,} tokens "
                   f"(saved {meta['tokens_saved']:,}) · {meta['secrets_redacted']} secrets masked")
        st.caption(f"Routing: {summary}")
        if mock:
            detail = meta.get("cloud_fallback_reason") or "CLOUD_PROVIDER=mock"
            st.warning(f"Mock answer — not a real cloud call. {detail}")
        if meta.get("files_selected"):
            st.caption("Context sent: " + ", ".join(f"`{f}`" for f in meta["files_selected"]))
    with st.expander("Why this route?"):
        a = meta["analysis"]
        st.write(f"Complexity **{a.get('complexity', '?')}** ({a.get('complexity_score_100', '?')}/100) "
                 f"→ `{a['decision']}` · initial route `{a.get('initial_route', '?')}`")
        if a.get("model_complexity"):
            st.caption(f"heuristic `{a.get('heuristic_complexity', '?')}` → local classifier "
                       f"`{a['model_complexity']}`"
                       + (f" — {a['model_complexity_reason']}" if a.get("model_complexity_reason") else ""))
        st.write(f"Repo searched: `{a.get('repo_path') or 'none'}` · " + "; ".join(a.get("context_sources") or []))
        st.write(a["factors"])
        for reason in a["reasons"]:
            st.write(f"- {reason}")
        if meta.get("capability_reasons"):
            st.write("Capability: " + "; ".join(meta["capability_reasons"]))
    cm = meta.get("context_metadata") or {}
    if cm.get("retrieval_coverage") or meta.get("files_selected"):
        with st.expander("Retrieved context"):
            cov = cm.get("retrieval_coverage") or {}
            files = meta.get("files_selected") or []
            st.write(f"Optimizer `{meta.get('context_optimizer')}` · files **{cov.get('files_selected', len(files))}** · "
                     f"candidates {cov.get('candidates', '?')} · adequacy **{cm.get('adequacy_status', 'n/a')}** · "
                     f"budget {cm.get('context_budget', '?')}"
                     + (" · semantic **on**" if cov.get("semantic") else ""))
            scope = cm.get("scope") or {}
            if scope.get("corpus_wide"):
                langs = ", ".join(scope.get("source_languages", [])) + (
                    " → " + ", ".join(scope.get("target_languages", [])) if scope.get("target_languages") else "")
                st.write(f"Scope: **corpus-wide** · {langs} · {cov.get('in_scope_files', '?')} files · "
                         f"full {cov.get('represented_full', '?')} · signatures {cov.get('represented_signature', '?')} · "
                         f"omitted {cov.get('omitted_files', '?')}")
            if cm.get("adequacy_recommendation"):
                st.warning(cm["adequacy_recommendation"])
            if cm.get("missing_required"):
                st.warning("Missing required context: " + ", ".join(cm["missing_required"]))
            if cm.get("uncertain_references"):
                st.caption("Uncertain references: " + ", ".join(cm["uncertain_references"]))
            if cm.get("evidence"):
                for e in cm["evidence"][:10]:
                    st.caption(f"`{e['file']}:{e['start_line']}` {e.get('symbol','')} "
                               f"[{e.get('retriever','')}] {e.get('reason','')}")


for i, msg in enumerate(st.session_state.messages):
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("meta"):
            render_meta(msg["meta"], i)


# ---------- Pending choice (complex task) ----------

pending = st.session_state.pending
if pending:
    a = pending["analysis"]
    with st.chat_message("assistant"):
        st.warning(f"**This looks like a complex task** (score {a['complexity_score']}). "
                   "LocalPilot recommends the **global (cloud) LLM**.")
        for reason in a["reasons"]:
            st.write(f"- {reason}")
        st.info(f"Global would send ~**{a['estimated_cloud_tokens']:,}** tokens "
                f"(instead of {a['original_tokens']:,} for the full context). "
                "Local sends nothing off your machine.")
        c1, c2, _ = st.columns([1, 1, 2])
        choose_local = c1.button("🖥️ Use local", use_container_width=True)
        choose_cloud = c2.button("☁️ Use global (recommended)", type="primary", use_container_width=True)
        if choose_local or choose_cloud:
            st.session_state.pending = None
            run_and_record(pending["request"], "local" if choose_local else "cloud", a)
            st.rerun()


# ---------- New prompt ----------

prompt = st.chat_input("Ask a coding question...", disabled=pending is not None)
if prompt:
    req = {"prompt": prompt, "code": code, "repo_path": repo_path or None}
    st.session_state.messages.append({"role": "user", "content": prompt, "meta": None})
    analysis = api_analyze(req)

    if analysis["decision"] == "cloud":
        st.session_state.pending = {"request": req, "analysis": analysis}
    else:
        # Simple (or borderline) task: stay local, just notify
        st.session_state.toast = "Simple task: using the local LLM"
        run_and_record(req, "local", analysis)
    st.rerun()
