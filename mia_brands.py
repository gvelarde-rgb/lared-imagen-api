"""
mia_brands.py — Blueprint con las rutas de MIA 93.7 y Globo 98.9
Montado dentro del servicio lared-imagen-api para consolidar todo
en una sola instancia de Render (un solo billing).

Rutas expuestas:
  /mia/rss-proxy        /mia/generar-imagen        /mia/debug-fetch
  /globo/rss-proxy      /globo/generar-imagen      /globo/debug-fetch

La lógica es idéntica a la del servicio mia-imagen-api original:
captcha SiteGround, soporte AVIF, generación de imagen branded.
"""

import hashlib
import io
import os
import re
import struct
import textwrap
import time

import requests
from flask import Blueprint, Response, jsonify, request, send_file
from PIL import Image, ImageDraw, ImageFont

try:
    import pillow_avif  # noqa: F401 – registers AVIF codec with Pillow
except ImportError:
    pass

mia_bp = Blueprint("mia_brands", __name__)

BRANDS = {
    "mia": {
        "name": "Mia 937",
        "cms_domain": "cms.mia937.com",
        "wp_api_base": "https://cms.mia937.com/wp-json/wp/v2",
        "logo_url": "https://www.mia937.com/logos/logo_mia.svg",
        "logo_type": "svg",
        "accent_color": (148, 50, 120),   # purple
        "fb_page_slug": "mia937",
    },
    "globo": {
        "name": "Globo 989",
        "cms_domain": "cms.globo989.com",
        "wp_api_base": "https://cms.globo989.com/wp-json/wp/v2",
        "logo_file": "globo_logo.png",
        "logo_type": "file",
        "accent_color": (30, 100, 200),    # blue
        "fb_page_slug": "radioglobo989",
    },
}

# Shared image settings
TITLE_BG = (255, 255, 255, 210)
TITLE_TEXT_COLOR = (30, 30, 30)
OUTPUT_W, OUTPUT_H = 1080, 1350

# Browser-like headers to bypass Siteground captcha/bot-detection
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "es-GT,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

# ---------------------------------------------------------------------------
# SiteGround Captcha Solver — solves JS proof-of-work challenge
# ---------------------------------------------------------------------------
_sg_sessions = {}  # Per-domain reusable sessions


def _solve_sg_challenge(challenge_str: str) -> str | None:
    """Solve SiteGround's SHA-1 proof-of-work challenge."""
    import base64

    difficulty = int(challenge_str.split(":", 1)[0])
    challenge_bytes = challenge_str.encode("utf-8")
    shift = 32 - difficulty

    counter = 0
    max_attempts = 10_000_000

    while counter < max_attempts:
        if counter == 0:
            counter_bytes = b'\x00'
        else:
            if counter > 16777215:
                byte_count = 4
            elif counter > 65535:
                byte_count = 3
            elif counter > 255:
                byte_count = 2
            else:
                byte_count = 1
            counter_bytes = counter.to_bytes(byte_count, 'big')

        combined = challenge_bytes + counter_bytes
        h = hashlib.sha1(combined).digest()

        first_word = struct.unpack('>I', h[:4])[0]
        if (first_word >> shift) == 0:
            return base64.b64encode(combined).decode('ascii')

        counter += 1

    return None


def _get_sg_session(domain: str) -> requests.Session:
    """Get a requests.Session that has solved the SiteGround captcha for a given domain."""
    global _sg_sessions

    if domain in _sg_sessions:
        try:
            r = _sg_sessions[domain].get(
                f"https://{domain}/wp-json/wp/v2/posts?per_page=1&_fields=id",
                timeout=10
            )
            if r.status_code == 200 and 'sgcaptcha' not in r.text:
                return _sg_sessions[domain]
        except Exception:
            pass

    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    r = session.get(f"https://{domain}/wp-json/wp/v2/posts?per_page=1&_fields=id", timeout=15)

    if r.status_code == 200 and 'sgcaptcha' not in r.text:
        _sg_sessions[domain] = session
        return session

    # Extract captcha redirect URL
    match = re.search(r'content="0;([^"]+)"', r.text)
    if not match:
        print(f"SG Captcha ({domain}): Could not find redirect URL")
        _sg_sessions[domain] = session
        return session

    captcha_path = match.group(1)
    captcha_url = f"https://{domain}{captcha_path}"

    r2 = session.get(captcha_url, timeout=15)

    challenge_match = re.search(r'const sgchallenge="([^"]+)"', r2.text)
    submit_match = re.search(r'const sgsubmit_url="([^"]+)"', r2.text)

    if not challenge_match or not submit_match:
        print(f"SG Captcha ({domain}): Could not extract challenge")
        _sg_sessions[domain] = session
        return session

    challenge = challenge_match.group(1)
    submit_url = submit_match.group(1)

    print(f"SG Captcha ({domain}): Solving challenge (difficulty={challenge.split(':')[0]})...")
    start_time = time.time()

    solution = _solve_sg_challenge(challenge)
    elapsed = time.time() - start_time

    if not solution:
        print(f"SG Captcha ({domain}): Failed to solve after {elapsed:.1f}s")
        _sg_sessions[domain] = session
        return session

    print(f"SG Captcha ({domain}): Solved in {elapsed:.1f}s")

    from urllib.parse import quote
    sep = "&" if "?" in submit_url else "?"
    solve_url = f"https://{domain}{submit_url}{sep}sol={quote(solution)}&s={int(elapsed*1000)}:1"

    r3 = session.get(solve_url, timeout=15, allow_redirects=True)
    print(f"SG Captcha ({domain}): Submit response {r3.status_code}")

    _sg_sessions[domain] = session
    return session


# ---------------------------------------------------------------------------
# Logo cache — per brand
# ---------------------------------------------------------------------------
_logos = {}


def get_logo(brand_key: str) -> Image.Image:
    """Download and cache a brand logo as a PIL Image (RGBA)."""
    global _logos
    if brand_key in _logos:
        return _logos[brand_key]

    brand = BRANDS[brand_key]

    try:
        if brand.get("logo_type") == "svg":
            import cairosvg
            svg_data = requests.get(brand["logo_url"], timeout=10).content
            png_data = cairosvg.svg2png(bytestring=svg_data, output_width=280)
            img = Image.open(io.BytesIO(png_data)).convert("RGBA")
        elif brand.get("logo_type") == "file":
            logo_path = os.path.join(os.path.dirname(__file__), brand["logo_file"])
            img = Image.open(logo_path).convert("RGBA")
            # Resize to reasonable logo size (max 280px wide)
            if img.width > 280:
                ratio = 280 / img.width
                img = img.resize((280, int(img.height * ratio)), Image.LANCZOS)
        else:
            logo_data = requests.get(brand["logo_url"], timeout=10).content
            img = Image.open(io.BytesIO(logo_data)).convert("RGBA")
            if img.width > 280:
                ratio = 280 / img.width
                img = img.resize((280, int(img.height * ratio)), Image.LANCZOS)
    except Exception as e:
        print(f"Logo load error ({brand_key}): {e}")
        img = Image.new("RGBA", (280, 120), (0, 0, 0, 0))

    _logos[brand_key] = img
    return img


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
def _guess_mime(url: str) -> str:
    if url.lower().endswith(".png"):
        return "image/png"
    if url.lower().endswith(".gif"):
        return "image/gif"
    if url.lower().endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def _build_rss(brand_key: str) -> Response:
    """Build an RSS feed from the WP REST API for a given brand."""
    import html as html_mod
    from datetime import datetime, timezone

    brand = BRANDS[brand_key]
    domain = brand["cms_domain"]
    api_base = brand["wp_api_base"]

    try:
        session = _get_sg_session(domain)
        api_url = f"{api_base}/posts?per_page=10&_embed"
        resp = session.get(api_url, timeout=20)
        resp.encoding = "utf-8"
        resp.raise_for_status()
        posts = resp.json()
    except Exception as e:
        return Response(f"Error fetching WP API: {e}", status=502, mimetype="text/plain")

    now_rfc822 = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    items_xml = []
    for post in posts:
        title = html_mod.unescape(post.get("title", {}).get("rendered", ""))
        link = post.get("link", "")
        excerpt = post.get("excerpt", {}).get("rendered", "")
        excerpt_text = re.sub(r"<[^>]+>", "", html_mod.unescape(excerpt)).strip()
        pub_date = ""
        if post.get("date_gmt"):
            try:
                dt = datetime.strptime(post["date_gmt"], "%Y-%m-%dT%H:%M:%S")
                pub_date = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except ValueError:
                pub_date = now_rfc822

        img_url = ""
        try:
            media_list = post.get("_embedded", {}).get("wp:featuredmedia", [])
            if media_list:
                img_url = media_list[0].get("source_url", "")
        except (IndexError, KeyError, TypeError):
            pass

        title_escaped = (
            title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        excerpt_escaped = (
            excerpt_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

        media_tag = ""
        if img_url:
            mime = _guess_mime(img_url)
            media_tag = f'<media:content url="{img_url}" type="{mime}" medium="image" />'

        items_xml.append(f"""    <item>
      <title>{title_escaped}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{link}</guid>
      <pubDate>{pub_date}</pubDate>
      <description>{excerpt_escaped}</description>
      {media_tag}
    </item>""")

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>{brand["name"]}</title>
    <link>https://{domain}</link>
    <description>Noticias de {brand["name"]}</description>
    <lastBuildDate>{now_rfc822}</lastBuildDate>
{chr(10).join(items_xml)}
  </channel>
</rss>"""

    return Response(rss, mimetype="application/rss+xml; charset=utf-8")


def _generate_branded_image(brand_key: str, titulo: str, foto_url: str) -> Image.Image:
    """Create a 1080x1350 branded image for any brand."""
    brand = BRANDS[brand_key]
    domain = brand["cms_domain"]
    accent = brand["accent_color"]

    # Download photo
    if domain in foto_url:
        session = _get_sg_session(domain)
        resp = session.get(foto_url, timeout=15)
    else:
        resp = requests.get(foto_url, headers=BROWSER_HEADERS, timeout=15)
    resp.raise_for_status()
    photo = Image.open(io.BytesIO(resp.content)).convert("RGB")

    # Create canvas
    canvas = Image.new("RGB", (OUTPUT_W, OUTPUT_H), (0, 0, 0))

    # Cover-fit the photo
    photo_ratio = photo.width / photo.height
    target_ratio = OUTPUT_W / OUTPUT_H
    if photo_ratio > target_ratio:
        new_h = OUTPUT_H
        new_w = int(new_h * photo_ratio)
    else:
        new_w = OUTPUT_W
        new_h = int(new_w / photo_ratio)
    photo = photo.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - OUTPUT_W) // 2
    top = (new_h - OUTPUT_H) // 2
    photo = photo.crop((left, top, left + OUTPUT_W, top + OUTPUT_H))
    canvas.paste(photo)

    # Create overlay layer
    overlay = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Title bar dimensions
    bar_margin = 60
    bar_top = OUTPUT_H - 480
    bar_bottom = OUTPUT_H - 200
    bar_left = bar_margin
    bar_right = OUTPUT_W - bar_margin

    draw.rectangle(
        [bar_left, bar_top, bar_right, bar_bottom],
        fill=TITLE_BG,
    )

    # Load font
    font_size = 42
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except OSError:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", font_size)
        except OSError:
            font = ImageFont.load_default()

    # Wrap and draw text
    max_chars = 30
    wrapped = textwrap.fill(titulo, width=max_chars)
    lines = wrapped.split("\n")

    total_text_height = len(lines) * (font_size + 10)
    text_y = bar_top + (bar_bottom - bar_top - total_text_height) // 2

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        text_x = (OUTPUT_W - tw) // 2
        draw.text((text_x, text_y), line, fill=TITLE_TEXT_COLOR, font=font)
        text_y += font_size + 10

    # Decorative corner brackets (brand color)
    bracket_len = 60
    bracket_w = 10
    bx1, by1 = bar_left - 10, bar_top - 10
    bx2, by2 = bar_right + 10, bar_bottom + 10

    draw.line([(bx1, by1), (bx1 + bracket_len, by1)], fill=accent + (255,), width=bracket_w)
    draw.line([(bx1, by1), (bx1, by1 + bracket_len)], fill=accent + (255,), width=bracket_w)
    draw.line([(bx2, by1), (bx2 - bracket_len, by1)], fill=accent + (255,), width=bracket_w)
    draw.line([(bx2, by1), (bx2, by1 + bracket_len)], fill=accent + (255,), width=bracket_w)
    draw.line([(bx1, by2), (bx1 + bracket_len, by2)], fill=accent + (255,), width=bracket_w)
    draw.line([(bx1, by2), (bx1, by2 - bracket_len)], fill=accent + (255,), width=bracket_w)
    draw.line([(bx2, by2), (bx2 - bracket_len, by2)], fill=accent + (255,), width=bracket_w)
    draw.line([(bx2, by2), (bx2, by2 - bracket_len)], fill=accent + (255,), width=bracket_w)

    # Composite overlay
    canvas = canvas.convert("RGBA")
    canvas = Image.alpha_composite(canvas, overlay)

    # Paste logo — centered vertically between bar_bottom and image bottom
    logo = get_logo(brand_key)
    logo_x = (OUTPUT_W - logo.width) // 2
    space_below = OUTPUT_H - bar_bottom
    logo_y = bar_bottom + (space_below - logo.height) // 2
    canvas.paste(logo, (logo_x, logo_y), logo)

    return canvas.convert("RGB")


# ---------------------------------------------------------------------------
# /  — Health check
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# MIA routes  (prefijo /mia)
# ---------------------------------------------------------------------------
@mia_bp.route("/mia/rss-proxy")
def mia_rss_proxy():
    return _build_rss("mia")


@mia_bp.route("/mia/generar-imagen")
def mia_generar_imagen():
    titulo = request.args.get("titulo", "")
    foto_url = request.args.get("foto_url", "")
    if not titulo or not foto_url:
        return Response("Faltan parametros: titulo, foto_url", status=400)
    try:
        img = _generate_branded_image("mia", titulo, foto_url)
    except Exception as e:
        return Response(f"Error generando imagen: {e}", status=500)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg", as_attachment=False, download_name="mia_noticia.jpg")


@mia_bp.route("/mia/debug-fetch")
def mia_debug_fetch():
    url = request.args.get("url", f"{BRANDS['mia']['wp_api_base']}/posts?per_page=1")
    try:
        session = _get_sg_session(BRANDS["mia"]["cms_domain"])
        resp = session.get(url, timeout=20)
        return jsonify({
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "body_preview": resp.text[:500],
        })
    except Exception as e:
        return jsonify({"error": str(e)})


# ---------------------------------------------------------------------------
# GLOBO routes  (prefijo /globo)
# ---------------------------------------------------------------------------
@mia_bp.route("/globo/rss-proxy")
def globo_rss_proxy():
    return _build_rss("globo")


@mia_bp.route("/globo/generar-imagen")
def globo_generar_imagen():
    titulo = request.args.get("titulo", "")
    foto_url = request.args.get("foto_url", "")
    if not titulo or not foto_url:
        return Response("Faltan parametros: titulo, foto_url", status=400)
    try:
        img = _generate_branded_image("globo", titulo, foto_url)
    except Exception as e:
        return Response(f"Error generando imagen: {e}", status=500)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    buf.seek(0)
    return send_file(buf, mimetype="image/jpeg", as_attachment=False, download_name="globo_noticia.jpg")


@mia_bp.route("/globo/debug-fetch")
def globo_debug_fetch():
    url = request.args.get("url", f"{BRANDS['globo']['wp_api_base']}/posts?per_page=1")
    try:
        session = _get_sg_session(BRANDS["globo"]["cms_domain"])
        resp = session.get(url, timeout=20)
        return jsonify({
            "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type", ""),
            "body_preview": resp.text[:500],
        })
    except Exception as e:
        return jsonify({"error": str(e)})
