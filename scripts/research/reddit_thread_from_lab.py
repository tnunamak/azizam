#!/usr/bin/env python3
"""Render a Reddit-style HTML thread from a 100x trial-lab JSON file."""

from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any


DEFAULT_LAB = Path("/tmp/ai-gateway-simulations/100x/latest/100x-trial-lab.json")
DEFAULT_OUT = Path(
    "/tmp/ai-gateway-simulations/100x/latest/simulated-reddit-thread-100x.html"
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def render_thread(data: dict[str, Any]) -> str:
    summaries = {item["id"]: item for item in data["scenario_summaries"]}
    results_by = data["results_by_scenario"]
    comments = build_comments(data, summaries, results_by)
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            '  <meta name="viewport" content="width=device-width, initial-scale=1">',
            "  <title>100x Simulated Reddit Thread - Vivid Fish AI Gateway</title>",
            "  <style>",
            CSS,
            "  </style>",
            "</head>",
            "<body>",
            '  <header class="topbar"><div><strong>reddit</strong><span> r/selfhosted · r/LocalLLaMA · r/comfyui</span></div></header>',
            '  <main class="wrap">',
            render_op(data, summaries),
            '    <section class="comments">',
            *[render_comment(comment) for comment in comments],
            "    </section>",
            "  </main>",
            "</body>",
            "</html>",
        ]
    )


CSS = """
    :root { color-scheme: light; --bg:#f6f7f8; --card:#fff; --text:#1f2328; --muted:#667085; --line:#d0d7de; --accent:#ff4500; --link:#0969da; --code:#f2f4f7; --good:#067647; --bad:#b42318; --warn:#b54708; }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--text); font-family: Arial, Helvetica, sans-serif; font-size: 14px; line-height: 1.45; }
    .topbar { position: sticky; top: 0; z-index: 2; border-bottom: 1px solid var(--line); background: #fff; padding: 10px 16px; }
    .topbar span { color: var(--muted); margin-left: 10px; }
    .wrap { width: min(1040px, calc(100vw - 24px)); margin: 16px auto 48px; }
    .post, .comment { display: grid; grid-template-columns: 44px 1fr; gap: 10px; background: var(--card); border: 1px solid var(--line); border-radius: 6px; margin-bottom: 10px; }
    .vote { display: flex; align-items: center; flex-direction: column; gap: 4px; padding: 12px 0; color: var(--muted); font-weight: 700; background: #f2f3f5; border-radius: 6px 0 0 6px; }
    .arrow { width: 0; height: 0; border-left: 7px solid transparent; border-right: 7px solid transparent; }
    .up { border-bottom: 10px solid #9aa4b2; } .down { border-top: 10px solid #9aa4b2; }
    .content { padding: 12px 14px 14px 0; min-width: 0; }
    .meta { color: var(--muted); font-size: 12px; }
    h1 { font-size: 22px; line-height: 1.25; margin: 8px 0 12px; letter-spacing: 0; }
    h2 { font-size: 15px; margin: 12px 0 6px; }
    p { margin: 8px 0; }
    ul { margin: 8px 0 8px 20px; padding: 0; }
    code { background: var(--code); border-radius: 4px; padding: 1px 4px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .flair { display:inline-block; margin-right: 8px; padding: 2px 6px; border-radius: 4px; background:#e7f5ff; color:#075985; font-size: 12px; font-weight:700; }
    .actions { display:flex; gap:14px; color:var(--muted); font-size:12px; font-weight:700; margin-top:10px; }
    .children { margin-left: 34px; padding-left: 12px; border-left: 2px solid var(--line); }
    .tag { font-size: 12px; font-weight: 700; }
    .good { color: var(--good); } .bad { color: var(--bad); } .warn { color: var(--warn); }
    @media (max-width: 640px) { .post, .comment { grid-template-columns: 34px 1fr; } .wrap { width: calc(100vw - 12px); } .children { margin-left: 16px; padding-left: 8px; } h1 { font-size: 19px; } }
"""


def render_op(data: dict[str, Any], summaries: dict[str, dict[str, Any]]) -> str:
    context = data["context"]
    passed = sum(1 for item in summaries.values() if item["status"].startswith("passed"))
    untested = sum(1 for item in summaries.values() if item["status"] == "untested")
    lines = [
        '    <article class="post">',
        '      <div class="vote"><div class="arrow up"></div><div>684</div><div class="arrow down"></div></div>',
        '      <div class="content">',
        '        <div class="meta">Posted by u/gateway_lab_rat 2 hours ago · edited 31 min. ago</div>',
        '        <h1><span class="flair">Project trial</span>I tested a self-hosted AI gateway. Runtime compatibility looks real, but I would not expect Reddit to be kind to it yet.</h1>',
        "        <p>I tested the gateway like a skeptical self-hosted/local-AI/media user would: first-run evidence, normal-user runtime key behavior, OpenAI-compatible chat and streaming, Anthropic Messages, Ollama-compatible routes, media workflow discovery, resources, receipts, and admin observability.</p>",
        f"        <p><strong>Result:</strong> {passed} scenario groups passed with live evidence; {untested} admin scenario is still unproven because no admin key was available in the run.</p>",
        "        <ul>",
        f"          <li>Gateway root: <code>{esc(context['base_url'])}</code>.</li>",
        f"          <li>Runtime key env used: <code>{esc(context.get('runtime_key_env') or 'missing')}</code>; admin key env: <code>{esc(context.get('admin_key_env') or 'missing')}</code>.</li>",
        "          <li>OpenAI: model list, non-streaming chat, and streaming chat returned 200. Streaming included content deltas and <code>[DONE]</code>.</li>",
        "          <li>Anthropic: model list, Messages, and count_tokens returned 200.</li>",
        "          <li>Ollama: tags, version, ps, chat returned 200; pull returned a fast 501 explaining that model-file mutation is unsupported.</li>",
        "          <li>Media: capabilities, workflows, extensions, resources, receipts, media jobs, and model-specific capability discovery returned 200.</li>",
        "        </ul>",
        "        <p>The thread below is calibrated to the reference discussions: most people do not try it, many skim and dismiss it, several misunderstand the use case, and only a minority engage with the actual evidence.</p>",
        '        <div class="actions"><span>142 comments</span><span>share</span><span>save</span><span>hide</span><span>report</span></div>',
        "      </div>",
        "    </article>",
    ]
    return "\n".join(lines)


def build_comments(
    data: dict[str, Any],
    summaries: dict[str, dict[str, Any]],
    results_by: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    add = comments.append
    add(comment("u/another-ai-wrapper", 421, "r/selfhosted", "So it is another proxy in front of Ollama/Open WebUI/LiteLLM/ComfyUI? Hard pass unless the docs show exactly why I need this extra moving part.", ["This is the average reaction. People will not read the trial lab before asking why it exists."]))
    add(comment("u/show_the_repo", 388, "r/selfhosted", "Where is the GitHub link, license, compose file, screenshots, and threat model? A wall of endpoint results is not how I decide what gets reverse-proxied into my house.", ["Yep. If the post starts with protocol trivia instead of install/trust basics, most self-hosters are gone."]))
    add(comment("u/not-installing-this", 351, "r/LocalLLaMA", "I am not installing a gateway to find out if it works with my gateway. Show LibreChat, Open WebUI, OpenAI SDK, Anthropic SDK, and Ollama CLI screenshots/logs or it is just curl theater.", ["Curl theater is the right insult here. The 100x lab improved the evidence, but Reddit will still call it curl theater."]))
    add(comment("u/keycloak-fatigue", 330, "r/selfhosted", "No admin key in the test means the most important part was not tested. Users/apps/profiles/audit/OIDC/service registry are the product if you are sharing access. Everything else is hobby plumbing.", ["This should be the top criticism. Runtime green checks do not prove the operator workflow."]))
    add(comment("u/why-not-litellm", 318, "r/LocalLLaMA", "Why not LiteLLM?", ["OP can answer media workflows and local policy, but a lot of commenters will never get past this."]))
    add(comment("u/why-not-openwebui", 301, "r/selfhosted", "Why not Open WebUI?", ["Same thing. If this is not a chat UI, the post needs to say that in the first sentence."]))
    add(comment("u/why-not-comfy-api", 286, "r/comfyui", "Why not just use the ComfyUI API directly?", ["For ComfyUI users this is the default objection. They already accept workflow JSON complexity."]))
    add(comment("u/marketing-smell", 273, "r/selfhosted", "The name and the phrase '100x lab' make this sound like a startup growth post. That alone will get it downvoted in r/selfhosted.", ["Correct. The artifact should not imply Reddit rewards the framing."]))
    add(comment("u/admin-or-bust", 261, "r/selfhosted", "Runtime key cannot administer the gateway. Cool. Can I revoke it? Can I see last used? Can I see what model it hit? Can I rotate secrets? If not, I do not care.", ["This is the actual buyer/admin question."]))
    add(comment("u/skipped-the-post", 244, "r/LocalLLaMA", "Didn't read all that. Does it support tool calling correctly or not?", ["It has not been proven in this lab. That answer would annoy but be honest."]))
    add(comment("u/toolcall-trauma", 231, "r/LocalLLaMA", "Local OpenAI-compatible servers break on tools and structured outputs constantly. Streaming 5 SSE chunks is not the hard part.", ["Exactly. Tool calls, JSON mode, bad schemas, and quants breaking output are closer to the reference threads."]))
    add(comment("u/model-list-weird", 219, "r/LocalLLaMA", "OpenAI summary says data 14 and models 1. Ollama says 8. Anthropic says 14. That is already confusing and I have not even made a request.", ["Could be explainable, but unexplained model inventory differences will look broken."]))
    add(comment("u/latency-nope", 207, "r/LocalLLaMA", "13 seconds for one tiny non-streaming reply and 23 seconds for streaming? I get that local models are slow, but that is not a good first impression.", ["Some serious users will understand cold starts. Many will just bounce."]))
    add(comment("u/homelab-skeptic", 198, "r/selfhosted", "I don't want another auth layer around tools that already barely behave. Every new dashboard is another backup, restore, secret, and CVE to care about.", ["This is much more Reddit-realistic than everyone volunteering to test."]))
    add(comment("u/profiles-sound-enterprisey", 184, "r/selfhosted", "Apps, profiles, role mappings, admin keys, runtime keys. This already sounds like work.", ["The UX has to hide that complexity or self-hosters will reject it before trying."]))
    add(comment("u/local-first-paranoid", 173, "r/selfhosted", "If there is any cloud fallback, I need a giant red OFF switch and receipts proving it never fired. Otherwise absolutely not.", ["The lab did not test cloud-denied policy. This needs to stay a gap."]))
    add(comment("u/media-claim-police", 162, "r/comfyui", "Media discovery passed is not media generation passed. Do not mention image/audio/video in the title unless you generated something and retrieved the asset.", ["Harsh, but fair. Discovery is plumbing evidence, not output evidence."]))
    add(comment("u/no-controlnet-no-care", 151, "r/comfyui", "Wake me when ControlNet/IP-Adapter readiness is real. A proxy that cannot tell me missing nodes/model paths before I submit is just another failure surface.", ["This maps directly to the ComfyUI corpus."]))
    add(comment("u/direct-comfy-user", 146, "r/comfyui", "ComfyUI users do not need an OpenAI-shaped image API. We need workflow JSON, queue, history, files, custom nodes, and useful errors.", ["The gateway should lean into workflow-native APIs, not image-API branding."]))
    add(comment("u/this-is-an-ad", 139, "r/selfhosted", "This reads like an ad pretending to be a benchmark.", ["Reddit will say this even if the evidence is honest."]))
    add(comment("u/no-screenshots-pass", 132, "r/selfhosted", "No screenshots of the admin UI? pass.", ["The thread artifact is not enough. A launch post needs visual/admin proof."]))
    add(comment("u/oidc-ptsd", 128, "r/selfhosted", "OIDC support means nothing until someone shows Authentik/Keycloak setup with callback URL, groups, logout, and recovery. Generic OIDC docs are a trap.", ["The lab only proved docs/pages exist, not a completed OIDC setup."]))
    add(comment("u/openwebui-or-libreichat", 121, "r/selfhosted", "Most people here want Open WebUI or LibreChat with sane auth, not an API gateway they have to explain to their family.", ["This points to product positioning: operator infra, not end-user app."]))
    add(comment("u/serious-but-not-trying", 116, "r/LocalLLaMA", "The Anthropic route is interesting. I still would not try it until the matrix shows actual Claude Code / aider / LangChain behavior.", ["A small number of people will be interested but still not install."]))
    add(comment("u/ollama-pull-confusion", 111, "r/LocalLLaMA", "If pull is unsupported, do not call it Ollama-compatible. Call it Ollama inference-compatible.", ["This is a wording problem that will repeat throughout the thread."]))
    add(comment("u/inference-compatible", 67, "r/LocalLLaMA", "I think inference-compatible is the right term. tags/version/ps/chat are useful; model mutation is intentionally not in scope.", ["Reasonable replies exist, but they are a minority."]))
    add(comment("u/cli-first", 103, "r/selfhosted", "I trust CLI + config files more than yet another admin dashboard. But if you ship a dashboard, it better cover all normal admin work.", ["This is a good Rich Hickey simplicity check: one source of truth, no fake facade."]))
    add(comment("u/dashboard-suspicion", 96, "r/selfhosted", "Admin dashboard untested is not a small gap. It is the product gap.", ["Yes. The current gateway evidence is runtime-heavy."]))
    add(comment("u/openai-compatible-sigh", 92, "r/LocalLLaMA", "Every local thing says OpenAI-compatible until you try tools, streaming, usage, system prompts, JSON schema, and errors.", ["The new lab added streaming and usage shape, but not tools/JSON schema/error matrix."]))
    add(comment("u/sd-users-wont-care", 88, "r/comfyui", "r/StableDiffusion will not care about your gateway unless it solves a painful ComfyUI problem. '37 capabilities' is not a painful problem.", ["The value prop needs to be failed-node diagnostics, job durability, multi-user policy, and headless workflow control."]))
    add(comment("u/too-many-abstractions", 84, "r/selfhosted", "This sounds like abstraction lasagna. OpenAI frontends, provider-native APIs, gateway intents, profiles, workflows, extensions. What is the simple thing?", ["Important. The architecture may be justified, but the post must not expose all layers at once."]))
    add(comment("u/single-user-here", 79, "r/LocalLLaMA", "I am a single user on one machine. I do not need profiles and OIDC. I need speed and model quality.", ["Many LocalLLaMA readers are not the target. The simulation should include non-target rejection."]))
    add(comment("u/family-admin", 75, "r/selfhosted", "The family/friend angle is the only part that makes me care. But then admin audit being untested kills it.", ["This is the right product tension."]))
    add(comment("u/security-review-when", 71, "r/selfhosted", "Homegrown API keys plus OIDC plus reverse proxy plus media assets sounds like security review territory. Who maintains this?", ["Trust and maintenance are common Reddit blockers."]))
    add(comment("u/repo-or-nope", 68, "r/selfhosted", "If it is not packaged, documented, and reproducible from a fresh compose file, it does not exist to me.", ["The lab says Dockerfile and compose exist. The post still needs to show the exact flow."]))
    add(comment("u/dockerfile-is-not-install", 63, "r/selfhosted", "A Dockerfile existing is not the same as a good install story.", ["True. The lab checked presence, not a fresh install run."]))
    add(comment("u/try-it-no", 59, "r/LocalLLaMA", "I have enough broken local AI wrappers already. I will wait for someone else to bleed on it first.", ["This sentiment should dominate more than willingness to try."]))
    add(comment("u/protocol-nerd", 56, "r/LocalLLaMA", "The SSE detail is actually useful: 5 events, 4 JSON, 2 content deltas, DONE. That is the sort of evidence I want. Now do tools.", ["A few high-signal technical users will engage deeply."]))
    add(comment("u/actual-client-matrix", 53, "r/LocalLLaMA", "Stop saying clients in prose. Make a table: OpenAI py, OpenAI JS, LiteLLM client, LangChain JS/Python, LibreChat, Open WebUI, Ollama CLI.", ["The next lab should be named-client, not route-shaped."]))
    add(comment("u/receipts-interesting", 49, "r/comfyui", "Receipts could be useful if they include workflow version, seed, params accepted/rejected, backend, timing, and asset URLs. If they are just request IDs, meh.", ["The runtime receipt route passed, but content quality still needs inspection."]))
    add(comment("u/assets-expire", 45, "r/comfyui", "Signed asset URL expiry and refresh behavior matters. A job result I cannot retrieve later is not durable.", ["Not tested. Should be in next media lab."]))
    add(comment("u/no-real-media", 42, "r/comfyui", "No image generated, no audio generated, no video generated. Then the media part is discovery only.", ["Correct, and the artifact must preserve that boundary."]))
    add(comment("u/misread-the-title", 39, "r/selfhosted", "So this hosts models?", ["No, it gateways to services. Reddit will have multiple comments like this."]))
    add(comment("u/misread-two", 37, "r/LocalLLaMA", "Does this replace Ollama?", ["No. Another predictable misunderstanding."]))
    add(comment("u/misread-three", 35, "r/comfyui", "Does this install ComfyUI nodes automatically?", ["Not proven here."]))
    add(comment("u/op-responds-too-much", 33, "r/selfhosted", "OP is replying like a maintainer. That always makes these posts feel less organic.", ["Good meta feedback. The simulated OP should not be universally welcomed."]))
    add(comment("u/downvote-admin-gap", 31, "r/selfhosted", "Posting before admin was tested was a mistake. That is the thing self-hosters care about.", ["This is harsh, but plausible."]))
    add(comment("u/runtime-is-real", 29, "r/LocalLLaMA", "Credit where due: OpenAI, Anthropic, and Ollama inference all passing in one run is not nothing.", ["Positive minority comment."]))
    add(comment("u/but-why", 27, "r/selfhosted", "I still do not understand the use case.", ["This should appear even after OP explains it."]))
    add(comment("u/use-case-answer", 13, "r/selfhosted", "Use case is one policy/auth/discovery layer over a pile of local AI services. Whether that is worth it depends on admin UX.", ["A concise helpful answer, but buried."]))
    add(comment("u/name-is-weird", 24, "r/selfhosted", "Vivid Fish sounds like a hosted SaaS, not something I would put in my homelab.", ["Naming/branding friction is real."]))
    add(comment("u/too-long-didnt-read", 22, "r/LocalLLaMA", "tl;dr?", ["Runtime works, admin untested, media not generated."]))
    add(comment("u/benchmarks-or-no", 20, "r/LocalLLaMA", "Where are benchmarks versus direct llama.cpp/Ollama/vLLM? If it adds latency, I need to know.", ["Latency skepticism is core."]))
    add(comment("u/explain-value", 18, "r/selfhosted", "The first paragraph should be: 'This is not a chat app. This is auth/policy/discovery for multiple AI backends.' Everything else is noise.", ["Good product copy feedback."]))
    add(comment("u/hard-earned-trust", 16, "r/selfhosted", "I would not expose this beyond LAN until I saw auth tests, session handling, key revocation, audit, and backup/restore.", ["Reasonable, likely common."]))
    add(comment("u/config-source", 15, "r/selfhosted", "Where is the source of truth? DB? YAML? env? If the dashboard and files disagree, I am out.", ["This maps directly to prior architecture concerns."]))
    add(comment("u/comfy-template-fatigue", 14, "r/comfyui", "Comfy wrappers always look good until a custom node is missing. Show that failure path.", ["High-signal media gap."]))
    add(comment("u/no-one-will-use-this-yet", 12, "r/LocalLLaMA", "Cool project, but no one sane is replacing their working setup with this yet.", ["This is probably closer to Reddit's median response."]))
    add(comment("u/fair-summary", 10, "r/selfhosted", "Harsh summary: interesting runtime gateway, not launch-ready operator product.", ["This should be the final takeaway."]))
    return comments


def comment(user: str, score: int, flair: str, body: str, replies: list[str] | None = None) -> dict[str, Any]:
    return {"user": user, "score": score, "flair": flair, "body": body, "replies": replies or []}


def render_comment(item: dict[str, Any]) -> str:
    lines = [
        '      <article class="comment">',
        f'        <div class="vote"><div class="arrow up"></div><div>{item["score"]}</div><div class="arrow down"></div></div>',
        '        <div class="content">',
        f'          <div class="meta">{esc(item["user"])} · {esc(item["flair"])} · simulated</div>',
        f"          <p>{item['body']}</p>",
        '          <div class="actions"><span>reply</span><span>share</span><span>save</span></div>',
        "        </div>",
        "      </article>",
    ]
    if item["replies"]:
        lines.append('      <div class="children">')
        for idx, reply in enumerate(item["replies"], start=1):
            lines.extend(
                [
                    '        <article class="comment">',
                    f'          <div class="vote"><div class="arrow up"></div><div>{max(3, item["score"] // (idx + 3))}</div><div class="arrow down"></div></div>',
                    '          <div class="content">',
                    f'            <div class="meta">u/reply-{idx}-{esc(item["user"]).replace("u/", "")} · nested</div>',
                    f"            <p>{reply}</p>",
                    '            <div class="actions"><span>reply</span><span>share</span></div>',
                    "          </div>",
                    "        </article>",
                ]
            )
        lines.append("      </div>")
    return "\n".join(lines)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def render_notes(data: dict[str, Any], out: Path) -> str:
    summaries = data["scenario_summaries"]
    lines = [
        "# 100x Simulated Reddit Thread Notes",
        "",
        f"- HTML: `{out}`",
        f"- Source lab: `{DEFAULT_LAB}`",
        f"- Scenario groups: {len(summaries)}",
        f"- Passed groups: {sum(1 for item in summaries if item['status'].startswith('passed'))}",
        f"- Untested groups: {sum(1 for item in summaries if item['status'] == 'untested')}",
        "",
        "Evidence represented:",
        "",
    ]
    for item in summaries:
        lines.append(
            f"- {item['label']}: {item['status']} ({item['passed']} passed, {item['failed']} failed, {item['skipped']} skipped)"
        )
    lines.extend(
        [
            "",
            "Reddit realism calibration:",
            "",
            "- Most commenters are low-intent and will not install anything.",
            "- Common first reactions are 'why not LiteLLM/Open WebUI/Ollama/direct ComfyUI', 'this is an ad', and 'show screenshots/client matrices'.",
            "- Many comments deliberately misread the use case; the artifact should not make every participant unusually thoughtful.",
            "- Positive comments are present but should be a minority and should usually demand stronger proof.",
            "- Admin/control-plane gaps should dominate the self-hosted reaction.",
            "",
            "Important boundaries:",
            "",
            "- Does not claim successful admin observability; admin key was missing.",
            "- Does not claim actual image/audio/video generation; media discovery and job surfaces were tested.",
            "- Does claim OpenAI non-streaming + streaming, Anthropic Messages/count_tokens, and Ollama tags/version/ps/chat/unsupported-pull live evidence.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lab", default=str(DEFAULT_LAB))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lab_path = Path(args.lab)
    out = Path(args.out)
    data = load_json(lab_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_thread(data))
    notes = out.with_name(out.stem + "-notes.md")
    notes.write_text(render_notes(data, out))
    print(out)
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
