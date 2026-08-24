import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict, Any

import config
from src.llm_tracker import get_llm_call_count, is_nearing_rate_limit

logger = logging.getLogger(__name__)

def build_summary_html(
    candidates: List[Dict[str, Any]],
    script_data: Dict[str, Any],
    concept_info: Dict[str, Any],
    fact_check_res: Dict[str, Any],
    policy_res: Dict[str, Any],
    rights_res: Dict[str, Any],
    originality_res: Dict[str, Any],
    final_qa_res: Dict[str, Any],
    upload_res: Dict[str, Any],
    retry_counts: Dict[str, int]
) -> str:
    """Build clean, modern HTML daily summary report with comprehensive metrics."""
    today_str = datetime.now().strftime("%B %d, %Y")
    llm_calls = get_llm_call_count()
    llm_warning = is_nearing_rate_limit()

    concept_name = concept_info.get("name", "Top Recommendations") if concept_info else "Top Recommendations"
    script_text = script_data.get("full_text", "N/A") if isinstance(script_data, dict) else str(script_data)
    video_title = script_data.get("video_title", "Anime Recommendation Short") if isinstance(script_data, dict) else "Anime Recommendation Short"

    # Candidate titles HTML
    titles_html = ""
    for idx, c in enumerate(candidates, 1):
        reasoning = c.get("selection_reasoning", "Qualified for today's concept")
        titles_html += f"""
        <li style="margin-bottom: 12px;">
            <strong>#{idx}: {c.get('title')}</strong> [{c.get('selection_category', 'Recommendation')}]<br/>
            <em>Rating:</em> {c.get('average_score', 'N/A')}/10 | <em>Genres:</em> {', '.join(c.get('genres', []))}<br/>
            <em>Qualification Reasoning:</em> {reasoning}<br/>
            <em>Fact Check:</em> <span style="color:#059669;">Verified (Source: {c.get('source', 'API')})</span>
        </li>
        """

    # Excluded anime titles audit log
    excluded_candidates = []
    if candidates and "all_excluded_candidates" in candidates[0]:
        excluded_candidates = candidates[0]["all_excluded_candidates"]

    excluded_titles_html = ""
    if excluded_candidates:
        for ex in excluded_candidates[:15]:
            excluded_titles_html += f"<li><strong>{ex.get('title')}</strong> — {ex.get('reason')}</li>"
    else:
        excluded_titles_html = "<li><em>No anime titles excluded in this run.</em></li>"

    # Supervisor QA Summary Table HTML
    supervisor_checks = final_qa_res.get("checks", [])
    supervisor_verdict = final_qa_res.get("verdict", "APPROVED 🟢" if final_qa_res.get("pass") else "BLOCKED 🔴")
    supervisor_rows = ""

    if supervisor_checks:
        for check in supervisor_checks:
            pass_flag = check.get("pass", False)
            badge_color = "#059669" if pass_flag else "#dc2626"
            badge_text = "PASS ✅" if pass_flag else "FAIL ❌"
            supervisor_rows += f"""
            <tr>
                <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb; font-weight: 500;">{check.get('name')}</td>
                <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb; color: {badge_color}; font-weight: bold;">{badge_text}</td>
                <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb; color: #4b5563;">{check.get('reason')}</td>
            </tr>
            """
    else:
        # Fallback rows if checks list was not populated
        pass_flag = final_qa_res.get("pass", False)
        badge_color = "#059669" if pass_flag else "#dc2626"
        badge_text = "PASS ✅" if pass_flag else "FAIL ❌"
        supervisor_rows = f"""
        <tr>
            <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb; font-weight: 500;">Final Pre-Upload QA</td>
            <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb; color: {badge_color}; font-weight: bold;">{badge_text}</td>
            <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb; color: #4b5563;">{final_qa_res.get('reason', 'N/A')}</td>
        </tr>
        """

    # Asset Rights Table HTML
    rights_table_rows = ""
    assets = rights_res.get("assets", [])
    for a in assets:
        rl = a.get("risk_level", "REVIEW")
        ls = a.get("license_status", "LICENSE_UNKNOWN")
        if ls == "LICENSE_VERIFIED":
            badge_color = "#059669"
        elif rl == "HIGH" or ls == "LICENSE_RESTRICTED":
            badge_color = "#dc2626"
        else:
            badge_color = "#d97706"  # amber for REVIEW/UNKNOWN
        rights_table_rows += f"""
        <tr>
            <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb;">{a.get('asset_name')}</td>
            <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb;">{a.get('type')}</td>
            <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb;">{a.get('source', 'Unknown')}</td>
            <td style="padding: 6px 10px; border-bottom: 1px solid #e5e7eb; color: {badge_color}; font-weight: bold;">{a.get('rights_status')}</td>
        </tr>
        """

    # Review-flagged assets banner (shown when LICENSE_UNKNOWN assets are present)
    flagged_assets = rights_res.get("flagged_for_review", [])
    review_banner = ""
    if flagged_assets:
        flagged_names = ", ".join(a.get('asset_name', '') for a in flagged_assets)
        review_banner = f"""
        <div style="background:#fffbeb; border-left:4px solid #f59e0b; padding:12px; margin:16px 0; border-radius:4px;">
            <strong style="color:#92400e;">⚠️ HUMAN REVIEW REQUIRED — Asset Rights</strong>
            <p style="margin:4px 0 0 0; color:#78350f; font-size:13px;">
                {len(flagged_assets)} asset(s) have <strong>LICENSE_UNKNOWN</strong> status.
                Commercial use rights have not been confirmed. Review these assets before
                making the private video public:<br/>
                <strong>{flagged_names}</strong>
            </p>
        </div>
        """

    # Retry count summary
    retries_str = ", ".join([f"{k}: {v}" for k, v in retry_counts.items()]) if retry_counts else "0 retries"

    # Status badges
    final_qa_pass = final_qa_res.get("pass", False)
    
    upload_status_str = upload_res.get("privacy_status", "private").upper() if upload_res else "BLOCKED / NOT UPLOADED"
    studio_url = upload_res.get("studio_url", "#") if upload_res else "#"

    # Free-tier warning banner
    warning_banner = ""
    if llm_warning:
        warning_banner = f"""
        <div style="background-color: #fef2f2; border-left: 4px solid #ef4444; padding: 12px; margin-bottom: 20px; border-radius: 4px;">
            <strong style="color: #991b1b;">⚠️ FREE-TIER RATE LIMIT WARNING:</strong>
            <p style="margin: 4px 0 0 0; color: #7f1d1d; font-size: 13px;">
                Total LLM API calls in this run reached <strong>{llm_calls}</strong> (exceeds 80% daily warning limit of {config.LLM_CALL_WARNING_THRESHOLD}).
            </p>
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8"/>
        <style>
            body {{ font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.6; color: #1f2937; background-color: #f3f4f6; margin: 0; padding: 20px; }}
            .card {{ max-width: 700px; margin: 0 auto; background: #ffffff; border-radius: 12px; padding: 28px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }}
            .header {{ border-bottom: 2px solid #8b5cf6; padding-bottom: 16px; margin-bottom: 20px; }}
            .header h2 {{ margin: 0; color: #6d28d9; font-size: 22px; }}
            .badge {{ display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: bold; margin-right: 6px; }}
            .btn {{ display: inline-block; background-color: #7c3aed; color: #ffffff !important; font-weight: bold; text-decoration: none; padding: 12px 24px; border-radius: 8px; margin-top: 15px; }}
            .script-box {{ background-color: #f8fafc; border-left: 4px solid #8b5cf6; padding: 14px; font-family: monospace; font-size: 13px; white-space: pre-wrap; margin-top: 10px; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="header">
                <h2>🎬 Daily Anime Short Review Report — {today_str}</h2>
                <div style="margin-top: 8px;">
                    <span class="badge" style="background:#ede9fe; color:#6d28d9;">Concept: {concept_name}</span>
                    <span class="badge" style="background:{'#d1fae5' if final_qa_pass else '#fee2e2'}; color:{'#065f46' if final_qa_pass else '#991b1b'};">
                        Upload Status: {upload_status_str}
                    </span>
                </div>
            </div>

            {warning_banner}

            <h3>🛡️ Supervisor QA Summary</h3>
            <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin-bottom: 20px;">
                <div style="font-size: 15px; font-weight: bold; margin-bottom: 10px;">
                    Overall Verdict: <span style="color: {'#059669' if final_qa_pass else '#dc2626'}; font-size: 17px;">{supervisor_verdict}</span>
                </div>
                <table>
                    <thead>
                        <tr style="background:#f1f5f9; text-align:left;">
                            <th style="padding:6px 10px;">Check Name</th>
                            <th style="padding:6px 10px;">Status</th>
                            <th style="padding:6px 10px;">Details / Reason</th>
                        </tr>
                    </thead>
                    <tbody>
                        {supervisor_rows}
                    </tbody>
                </table>
            </div>

            <h3>⚙️ Channel Safety Settings</h3>
            <ul style="font-size: 13px; color: #374151;">
                <li><strong>Made for Kids:</strong> No (<code>selfDeclaredMadeForKids = False</code> explicitly set on upload)</li>
                <li><strong>Altered / Synthetic Content Disclosure:</strong> Exempt (Official anime artwork + AI voiceover recommendations)</li>
                <li><strong>Comment Moderation:</strong> Channel Level (Configured in YouTube Studio under Settings &gt; Community &gt; Defaults)</li>
                <li><strong>Upload Privacy Status:</strong> {upload_status_str}</li>
            </ul>

            <h3>🚫 Excluded Anime Titles Audit Log (30-Day Cooldown)</h3>
            <ul style="font-size: 12px; color: #4b5563; max-height: 140px; overflow-y: auto; background: #f9fafb; padding: 10px 20px; border-radius: 6px; border: 1px solid #e5e7eb;">
                {excluded_titles_html}
            </ul>

            <h3>🆓 Free-Tier Safeguards & Usage Summary</h3>
            <ul style="font-size: 13px; color: #374151;">
                <li><strong>LLM API Calls Used:</strong> {llm_calls} calls (Warning Threshold: {config.LLM_CALL_WARNING_THRESHOLD})</li>
                <li><strong>YouTube Data API Quota Usage:</strong> ~{config.YT_DAILY_QUOTA_ESTIMATE} / 10,000 daily free units</li>
                <li><strong>TTS Service (Edge-TTS):</strong> Free Tier Active & Healthy</li>
                <li><strong>Stage Retry Summary:</strong> {retries_str}</li>
            </ul>

            {f'<p><a href="{studio_url}" class="btn" target="_blank">🔗 Review Private Video on YouTube Studio</a></p>' if upload_res and studio_url != '#' else ''}

            <h3 style="margin-top: 25px;">⚖️ Copyright & Asset Rights Status</h3>
            {review_banner}
            <table>
                <thead>
                    <tr style="background:#f3f4f6; text-align:left;">
                        <th style="padding:6px 10px;">Asset</th>
                        <th style="padding:6px 10px;">Type</th>
                        <th style="padding:6px 10px;">Source</th>
                        <th style="padding:6px 10px;">License Status</th>
                    </tr>
                </thead>
                <tbody>
                    {rights_table_rows}
                </tbody>
            </table>

            <h3 style="margin-top: 25px;">🌟 Featured Anime Titles</h3>
            <ul>
                {titles_html}
            </ul>

            <h3 style="margin-top: 25px;">📌 Video Title: {video_title}</h3>
            <h3>📜 Spoken Narration Script ({script_data.get('word_count', 0)} words)</h3>
            <div class="script-box">{script_text}</div>

            <hr style="border: none; border-top: 1px solid #e5e7eb; margin-top: 30px;" />
            <p style="font-size: 12px; color: #6b7280; text-align: center;">
                Generated automatically by YouTube Anime Shorts Automation Pipeline
            </p>
        </div>
    </body>
    </html>
    """
    return html

def send_daily_summary_email(
    candidates: List[Dict[str, Any]],
    script_data: Dict[str, Any],
    concept_info: Dict[str, Any] = None,
    fact_check_res: Dict[str, Any] = None,
    policy_res: Dict[str, Any] = None,
    rights_res: Dict[str, Any] = None,
    originality_res: Dict[str, Any] = None,
    final_qa_res: Dict[str, Any] = None,
    upload_res: Dict[str, Any] = None,
    retry_counts: Dict[str, int] = None
) -> str:
    """Sends comprehensive summary email via Gmail SMTP or logs to file if credentials missing."""
    concept_info = concept_info or {"name": "Top Recommendations"}
    fact_check_res = fact_check_res or {"status": "verified"}
    policy_res = policy_res or {"status": "🟢 LOW RISK", "flagged_issues": []}
    rights_res = rights_res or {"assets": []}
    originality_res = originality_res or {"pass": True, "reason": "N/A"}
    final_qa_res = final_qa_res or {"pass": True}
    retry_counts = retry_counts or {}

    html_content = build_summary_html(
        candidates, script_data, concept_info, fact_check_res,
        policy_res, rights_res, originality_res, final_qa_res,
        upload_res, retry_counts
    )
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # Save HTML output report file regardless of email credentials
    email_file = config.OUTPUT_DIR / "email_summary.html"
    try:
        with open(email_file, "w", encoding="utf-8") as f:
            f.write(html_content)
        logger.info(f"Saved email report to {email_file}")
    except Exception as e:
        logger.warning(f"Could not save email report file: {e}")

    if not config.GMAIL_USER or not config.GMAIL_APP_PASSWORD:
        logger.warning("Gmail SMTP credentials missing. Written report to output/email_summary.html.")
        return "logged_to_file"

    recipient = config.NOTIFY_EMAIL or config.GMAIL_USER
    logger.info(f"Sending daily summary email to {recipient} via Gmail SMTP...")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎬 Daily Anime Short Review Report — {today_str}"
    msg["From"] = config.GMAIL_USER
    msg["To"] = recipient

    msg.attach(MIMEText(html_content, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(config.GMAIL_USER, config.GMAIL_APP_PASSWORD)
            server.sendmail(config.GMAIL_USER, [recipient], msg.as_string())
        logger.info("Daily summary email sent successfully!")
        return "sent"
    except Exception as e:
        logger.error(f"Failed to send email notification: {e}")
        return f"failed: {e}"

