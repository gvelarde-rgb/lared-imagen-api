"""
API de generación de imágenes para La Red 106.1
Endpoint: POST /generar-imagen
Body (application/json o form-data):
  - foto_url: URL de la imagen del artículo (la API la descarga)
  - titulo: texto del título de la noticia
Respuesta: { "imagen_url": "https://res.cloudinary.com/..." }
"""

from flask import Flask, request, jsonify, send_file, Response
from PIL import Image, ImageDraw, ImageFont
import requests
import io
import cloudinary
import cloudinary.uploader
import os
import hashlib
import base64
import threading
import time
from email.utils import formatdate, parsedate_to_datetime
from datetime import datetime, timezone

app = Flask(__name__)

# Registrar las rutas de MIA 93.7 y Globo 98.9 (prefijos /mia y /globo)
# Consolidadas en este mismo servicio para usar un solo billing de Render.
from mia_brands import mia_bp
app.register_blueprint(mia_bp)

# Configuración de Cloudinary desde variables de entorno
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", "dd9cuovet"),
    api_key=os.environ.get("CLOUDINARY_API_KEY", "436436496366223"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", "8-FsTEgnIoUkHOG_Vu1ESlV7c0c"),
    secure=True
)

# Dimensiones de la imagen final
W, H = 1080, 1350

# Ruta de la fuente (LiberationSans-Bold)
FONT_PATH = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"

# Coordenadas del diseño (medidas sobre la imagen original de Canva 1080x1350)
PANEL_X1, PANEL_Y1 = 60, 820
PANEL_X2, PANEL_Y2 = 1020, 1065
TEXT_X_CENTER = W // 2
TEXT_AREA_X1 = PANEL_X1 + 80
TEXT_AREA_X2 = PANEL_X2 - 80
TEXT_AREA_Y1 = PANEL_Y1 + 25
TEXT_AREA_Y2 = PANEL_Y2 - 20
TEXT_MAX_W = TEXT_AREA_X2 - TEXT_AREA_X1   # 820px
TEXT_MAX_H = TEXT_AREA_Y2 - TEXT_AREA_Y1   # 220px
LOGO_CX = W // 2
LOGO_CY = 1118
RED = (220, 30, 30, 255)

# Ruta al logo real
LOGO_PATH = os.path.join(os.path.dirname(__file__), "logo_lared.png")

# Cache del logo en memoria (se carga una sola vez)
_logo_cache = None


def get_logo():
    """Carga el logo real desde disco y lo cachea en memoria."""
    global _logo_cache
    if _logo_cache is None:
        _logo_cache = Image.open(LOGO_PATH).convert("RGBA")
    return _logo_cache

# Headers de navegador para evitar bloqueos de captcha
# Headers MÍNIMOS — Sucuri bloquea 403 desde IPs datacenter cuando ve
# headers completos de navegador (Accept, Referer, Accept-Language).
# Con headers mínimos (solo User-Agent + Accept-Encoding: identity) responde 200.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept-Encoding": "identity",
}


def avif_to_jpeg_bytes(raw_bytes):
    """Convierte AVIF → JPEG usando ffmpeg (cuando Pillow no soporta AVIF nativo)"""
    import subprocess, tempfile, os
    with tempfile.NamedTemporaryFile(suffix=".avif", delete=False) as f_in:
        f_in.write(raw_bytes)
        f_in_path = f_in.name
    f_out_path = f_in_path.replace(".avif", ".jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", f_in_path, "-q:v", "2", f_out_path],
            check=True, capture_output=True, timeout=20
        )
        with open(f_out_path, "rb") as fout:
            return fout.read()
    finally:
        for p in [f_in_path, f_out_path]:
            try:
                os.unlink(p)
            except Exception:
                pass


def descargar_imagen_url(url):
    """Descarga una imagen desde una URL usando headers de navegador.
    Falla rápido si el servidor responde con captcha/challenge (202 + sg-captcha).

    IMPORTANTE: allow_redirects=False en el primer request para poder detectar
    el 202 de Sucuri SG Captcha antes de que requests siga la meta-refresh y
    quede bloqueado indefinidamente en /.well-known/sgcaptcha/.
    """
    # Paso 1: request sin seguir redirects — detectar captcha/anti-bot al instante
    probe = requests.get(url, headers=BROWSER_HEADERS, timeout=8,
                         allow_redirects=False)

    # Sucuri SG Captcha: HTTP 202 + header sg-captcha (cms.lared1061.com)
    if probe.status_code == 202 or "sg-captcha" in probe.headers:
        raise ValueError(f"Captcha/anti-bot detectado en {url} (HTTP {probe.status_code})")

    # Paso 2: si pasó el probe (3xx redirect legítimo o 200 parcial), hacer GET completo
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=20, allow_redirects=True)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        raise ValueError(
            f"URL no devolvió imagen. Content-Type: {content_type!r} — URL: {url}"
        )
    if len(resp.content) < 500:
        raise ValueError(
            f"Imagen demasiado pequeña o vacía ({len(resp.content)} bytes) — URL: {url}"
        )
    return resp.content


def recortar_fill(img, tw, th):
    """Recorta la imagen al tamaño objetivo manteniendo el centro (tipo c_fill)"""
    ow, oh = img.size
    scale = max(tw / ow, th / oh)
    nw, nh = int(ow * scale), int(oh * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - tw) // 2
    top = (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def wrap_text_pixels(draw, texto, font, max_width):
    """Divide el texto en líneas que caben en max_width píxeles"""
    words = texto.split()
    lines = []
    current_line = []
    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
                current_line = [word]
            else:
                lines.append(word)
    if current_line:
        lines.append(" ".join(current_line))
    return lines


def generar_imagen(foto_bytes, titulo):
    """
    Genera la imagen de la noticia con:
    - Foto de fondo recortada a 1080x1350
    - Panel blanco semitransparente
    - Corchetes rojos decorativos
    - Título de la noticia en negrita centrado
    - Logo real de La Red 106.1 (PNG con transparencia)
    Devuelve la URL pública de Cloudinary.
    """
    # 1. Cargar y recortar foto de fondo
    try:
        foto = Image.open(io.BytesIO(foto_bytes)).convert("RGBA")
    except Exception as pil_err:
        # Fallback: convertir con ffmpeg (necesario para AVIF en python:3.11-slim)
        try:
            jpeg_bytes = avif_to_jpeg_bytes(foto_bytes)
            foto = Image.open(io.BytesIO(jpeg_bytes)).convert("RGBA")
        except Exception as ffmpeg_err:
            raise ValueError(
                f"No se pudo abrir la imagen ({len(foto_bytes)} bytes). "
                f"Pillow: {pil_err} | ffmpeg: {ffmpeg_err}"
            ) from pil_err
    fondo = recortar_fill(foto, W, H)

    # 2. Crear capa de overlay (panel + corchetes + logo)
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Panel blanco semitransparente
    draw.rectangle([(PANEL_X1, PANEL_Y1), (PANEL_X2, PANEL_Y2)], fill=(255, 255, 255, 210))

    # Corchetes rojos
    BLEN, BTHICK = 55, 9
    bx, by = PANEL_X1 + 18, PANEL_Y1 + 18
    draw.rectangle([(bx, by), (bx + BTHICK, by + BLEN)], fill=RED)
    draw.rectangle([(bx, by), (bx + BLEN, by + BTHICK)], fill=RED)
    bx2, by2 = PANEL_X2 - 18, PANEL_Y2 - 18
    draw.rectangle([(bx2 - BTHICK, by2 - BLEN), (bx2, by2)], fill=RED)
    draw.rectangle([(bx2 - BLEN, by2 - BTHICK), (bx2, by2)], fill=RED)

    # (logo PNG se pega después de alpha_composite — ver más abajo)

    # 3. Componer fondo + overlay
    canvas = Image.alpha_composite(fondo, overlay)

    # 3b. Pegar logo real (PNG con transparencia)
    logo = get_logo().copy()
    LOGO_TARGET_W = 340  # ancho del logo en la imagen final
    ratio = LOGO_TARGET_W / logo.width
    logo_resized = logo.resize(
        (LOGO_TARGET_W, int(logo.height * ratio)),
        Image.LANCZOS,
    )
    lx = (W - logo_resized.width) // 2
    ly = LOGO_CY - logo_resized.height // 2
    canvas.paste(logo_resized, (lx, ly), logo_resized)  # alpha del PNG como máscara

    draw_final = ImageDraw.Draw(canvas)

    # 4. Calcular fuente y líneas del título
    font_size = 58
    font = None
    lines = None
    line_h = None

    while font_size >= 28:
        try:
            f = ImageFont.truetype(FONT_PATH, font_size)
        except OSError:
            f = ImageFont.load_default()
        ls = wrap_text_pixels(draw_final, titulo, f, TEXT_MAX_W)
        bbox = draw_final.textbbox((0, 0), ls[0] if ls else "A", font=f)
        lh = bbox[3] - bbox[1]
        spacing = int(lh * 0.18)
        total_h = len(ls) * lh + (len(ls) - 1) * spacing
        if total_h <= TEXT_MAX_H:
            font = f
            lines = ls
            line_h = lh + spacing
            break
        font_size -= 3

    if font is None:
        try:
            font = ImageFont.truetype(FONT_PATH, 28)
        except OSError:
            font = ImageFont.load_default()
        lines = wrap_text_pixels(draw_final, titulo, font, TEXT_MAX_W)
        line_h = 35

    # Centrar verticalmente el texto en el panel
    bbox = draw_final.textbbox((0, 0), lines[0], font=font)
    lh_real = bbox[3] - bbox[1]
    spacing = line_h - lh_real
    total_h = len(lines) * lh_real + (len(lines) - 1) * spacing
    text_y = (TEXT_AREA_Y1 + TEXT_AREA_Y2) // 2 - total_h // 2

    for i, line in enumerate(lines):
        bbox = draw_final.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        tx = TEXT_X_CENTER - tw // 2
        ty = text_y + i * (lh_real + spacing)
        draw_final.text((tx, ty), line, fill=(20, 20, 20, 255), font=font)

    # 5. Guardar en buffer
    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, "JPEG", quality=92)
    buffer.seek(0)
    imagen_bytes = buffer.getvalue()

    # Subir a Cloudinary en background (no bloquea la respuesta a Make)
    def upload_cloudinary(img_bytes, pub_id):
        try:
            cloudinary.uploader.upload(
                io.BytesIO(img_bytes),
                public_id=pub_id,
                overwrite=True,
                resource_type="image",
                timeout=60
            )
        except Exception as e:
            app.logger.error(f"Cloudinary background upload failed: {str(e)[:100]}")

    titulo_hash = hashlib.md5(titulo.encode()).hexdigest()[:12]
    public_id = f"lared/noticia_{titulo_hash}"
    t = threading.Thread(target=upload_cloudinary, args=(imagen_bytes, public_id), daemon=True)
    t.start()

    return imagen_bytes


def generar_imagen_sin_foto(titulo):
    """
    Genera imagen completa cuando no hay foto disponible:
    fondo blanco, logo centrado arriba, título grande centrado,
    corchetes rojos decorativos.
    """
    canvas = Image.new("RGBA", (W, H), (255, 255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    RED_FILL = (220, 30, 30, 255)

    # --- Pre-calcular tamaño del texto para centrar todo el bloque verticalmente ---
    text_max_w = W - 120  # más ancho: 60px margen cada lado
    font_size = 96  # arranca más grande
    font = None
    lines = []
    while font_size >= 48:
        try:
            f = ImageFont.truetype(FONT_PATH, font_size)
        except OSError:
            f = ImageFont.load_default()
        ls = wrap_text_pixels(draw, titulo, f, text_max_w)
        bbox_a = draw.textbbox((0, 0), "A", font=f)
        lh = bbox_a[3] - bbox_a[1]
        spacing = int(lh * 0.22)
        total_h = len(ls) * lh + max(0, len(ls) - 1) * spacing
        if total_h <= H * 0.45:   # texto no ocupa más del 45% del alto
            font = f
            lines = ls
            break
        font_size -= 4
    if font is None:
        try:
            font = ImageFont.truetype(FONT_PATH, 48)
        except OSError:
            font = ImageFont.load_default()
        lines = wrap_text_pixels(draw, titulo, font, text_max_w)

    bbox0 = draw.textbbox((0, 0), "A", font=font)
    lh_real = bbox0[3] - bbox0[1]
    spacing = int(lh_real * 0.22)
    total_text_h = len(lines) * lh_real + max(0, len(lines) - 1) * spacing

    # --- Logo: recortar padding transparente con Pillow, escalar grande ---
    logo = get_logo().copy()
    # getbbox() devuelve el bounding box del contenido no-transparente
    bbox_logo = logo.getbbox()  # (left, upper, right, lower)
    if bbox_logo:
        logo_crop = logo.crop(bbox_logo)
    else:
        logo_crop = logo
    # Escalar: ancho fijo de 500px
    LOGO_TARGET_W = 500
    ratio = LOGO_TARGET_W / logo_crop.width
    logo_r = logo_crop.resize(
        (LOGO_TARGET_W, int(logo_crop.height * ratio)), Image.LANCZOS
    )

    GAP = 80  # espacio entre logo y texto
    block_h = logo_r.height + GAP + total_text_h
    block_y = max(80, (H - block_h) // 2 - 80)  # ligeramente sobre el centro óptico

    lx = (W - logo_r.width) // 2
    ly = block_y
    canvas.paste(logo_r, (lx, ly), logo_r)

    text_area_y1 = ly + logo_r.height + GAP
    text_max_w = W - 160  # 80px margen cada lado

    # Posición Y del texto (justo debajo del logo con el GAP)
    text_y = text_area_y1

    # Corchetes rojos alrededor del texto
    pad = 30
    bx1 = (W - text_max_w) // 2 - pad
    by1 = text_y - pad
    bx2 = bx1 + text_max_w + pad * 2
    by2 = text_y + total_h + pad
    BLEN, BTHICK = 55, 8
    # TL
    draw.rectangle([(bx1, by1), (bx1 + BLEN, by1 + BTHICK)], fill=RED_FILL)
    draw.rectangle([(bx1, by1), (bx1 + BTHICK, by1 + BLEN)], fill=RED_FILL)
    # TR
    draw.rectangle([(bx2 - BLEN, by1), (bx2, by1 + BTHICK)], fill=RED_FILL)
    draw.rectangle([(bx2 - BTHICK, by1), (bx2, by1 + BLEN)], fill=RED_FILL)
    # BL
    draw.rectangle([(bx1, by2 - BTHICK), (bx1 + BLEN, by2)], fill=RED_FILL)
    draw.rectangle([(bx1, by2 - BLEN), (bx1 + BTHICK, by2)], fill=RED_FILL)
    # BR
    draw.rectangle([(bx2 - BLEN, by2 - BTHICK), (bx2, by2)], fill=RED_FILL)
    draw.rectangle([(bx2 - BTHICK, by2 - BLEN), (bx2, by2)], fill=RED_FILL)

    # Texto
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        tx = (W - tw) // 2
        ty = text_y + i * (lh_real + spacing)
        draw.text((tx, ty), line, fill=(20, 20, 20, 255), font=font)

    return canvas.convert("RGB")


def get_foto_bytes(foto_url):
    """
    Descarga foto_url con múltiples fallbacks.
    - Vacío → None
    - Descarga directa OK → bytes
    - Captcha/anti-bot (cms.lared1061.com bloqueado desde Render) → weserv.nl proxy → bytes
    - weserv falla → Cloudinary upload proxy → bytes
    - Todo falla → None (imagen sin foto)

    NOTA: cms.lared1061.com tiene Sucuri SG Captcha que bloquea IPs de datacenter
    (Render, AWS, GCP, etc.) con HTTP 202. weserv.nl es un proxy de imágenes gratuito
    cuyos servidores no están en la lista negra de Sucuri.
    """
    if not foto_url:
        return None

    # Intento 1: descarga directa
    try:
        return descargar_imagen_url(foto_url)
    except Exception as e1:
        app.logger.warning(f"Descarga directa falló: {str(e1)[:80]}")

    # Fallback 1: weserv.nl como proxy de imágenes
    # Sus IPs no están bloqueadas por Sucuri. Funciona con cms.lared1061.com.
    try:
        from urllib.parse import urlparse, quote
        # weserv acepta la URL sin protocolo: strip https://
        url_sin_proto = foto_url.replace("https://", "").replace("http://", "")
        weserv_url = f"https://images.weserv.nl/?url={url_sin_proto}&output=jpg&maxage=1d"
        resp = requests.get(weserv_url, headers={"User-Agent": "Mozilla/5.0"},
                            timeout=20, allow_redirects=True)
        ct = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and ct.startswith("image/") and len(resp.content) > 500:
            app.logger.info(f"Foto via weserv.nl OK ({len(resp.content)} bytes)")
            return resp.content
        else:
            raise ValueError(f"weserv status={resp.status_code} ct={ct} bytes={len(resp.content)}")
    except Exception as e_weserv:
        app.logger.warning(f"weserv.nl proxy falló: {str(e_weserv)[:80]}")

    # Fallback 2: Cloudinary upload como proxy (último recurso)
    try:
        result = cloudinary.uploader.upload(
            foto_url,
            public_id="lared/proxy_foto",
            overwrite=True,
            resource_type="image",
            timeout=20
        )
        resp = requests.get(result["secure_url"],
                            headers=BROWSER_HEADERS, timeout=15)
        resp.raise_for_status()
        app.logger.info(f"Foto via Cloudinary proxy OK ({len(resp.content)} bytes)")
        return resp.content
    except Exception as e2:
        app.logger.warning(f"Cloudinary proxy falló: {str(e2)[:80]}")
        return None


def generar_imagen_bytes(foto_bytes, titulo):
    """Alias de generar_imagen que devuelve bytes"""
    return generar_imagen(foto_bytes, titulo)


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "servicio": "La Red - Generador de Imágenes", "version": "4e6518a-identity"})


@app.route("/generar-imagen", methods=["GET", "POST"])
def generar():
    """
    Endpoint principal. Acepta tres formatos:

    1. application/json con URL:
       - foto_url: URL de la imagen (la API la descarga)
       - titulo: texto del título

    2. application/json con base64:
       - foto_base64: imagen codificada en base64
       - titulo: texto del título

    3. multipart/form-data:
       - foto: archivo binario de la imagen
       - titulo: texto del título
    """
    titulo = None
    foto_bytes = None

    # Soporte GET con query parameters
    if request.method == "GET":
        titulo = request.args.get("titulo", "").strip()
        foto_url = request.args.get("foto_url", "").strip()
        return_json = request.args.get("json", "false").lower() == "true"

        if not titulo:
            return jsonify({"error": "Se requiere el campo 'titulo'"}), 400

        foto_bytes = get_foto_bytes(foto_url)

        # Si no hay foto, generar imagen solo-texto
        if not foto_bytes:
            img_sin_foto = generar_imagen_sin_foto(titulo)
            buf = io.BytesIO()
            img_sin_foto.save(buf, "JPEG", quality=92)
            imagen_bytes = buf.getvalue()
        else:
            try:
                imagen_bytes = generar_imagen_bytes(foto_bytes, titulo)
            except Exception:
                # Fallback: siempre devolver imagen con diseño aunque falle la foto
                img_sin_foto = generar_imagen_sin_foto(titulo)
                buf = io.BytesIO()
                img_sin_foto.save(buf, "JPEG", quality=92)
                imagen_bytes = buf.getvalue()

        # Devolver imagen binaria inmediatamente (Cloudinary ya subió en background)
        if return_json:
            # Solo para debug: esperar Cloudinary y devolver URL
            titulo_hash = hashlib.md5(titulo.encode()).hexdigest()[:12]
            try:
                r = cloudinary.uploader.upload(
                    io.BytesIO(imagen_bytes),
                    public_id=f"lared/noticia_{titulo_hash}",
                    overwrite=True, resource_type="image", timeout=35
                )
                return jsonify({"imagen_url": r["secure_url"], "ok": True})
            except Exception as e:
                return jsonify({"error": str(e), "ok": False}), 500

        return send_file(
            io.BytesIO(imagen_bytes),
            mimetype='image/jpeg',
            as_attachment=False,
            download_name='lared_noticia.jpg'
        )

    content_type = request.content_type or ""

    if "application/octet-stream" in content_type:
        # Make envía los bytes directamente con el título en el header X-Titulo
        titulo = request.headers.get("X-Titulo", "").strip()
        foto_bytes = request.get_data()

    elif "multipart/form-data" in content_type:
        titulo = request.form.get("titulo", "").strip()
        foto_url = request.form.get("foto_url", "").strip()
        if foto_url:
            foto_bytes = descargar_imagen_url(foto_url)
        elif "foto" in request.files:
            foto_bytes = request.files["foto"].read()

    elif "application/x-www-form-urlencoded" in content_type:
        titulo = request.form.get("titulo", "").strip()
        foto_url = request.form.get("foto_url", "").strip()
        if foto_url:
            foto_bytes = descargar_imagen_url(foto_url)

    else:
        # JSON (por defecto)
        data = request.get_json(force=True) or {}
        titulo = (data.get("titulo") or "").strip()
        # FIX 2: foto_url puede llegar como null/None desde Make — no llamar .strip() sobre None
        foto_url = (data.get("foto_url") or "").strip()
        foto_b64 = (data.get("foto_base64") or "").strip()

        if foto_url:
            foto_bytes = get_foto_bytes(foto_url)
        elif foto_b64:
            if "," in foto_b64:
                foto_b64 = foto_b64.split(",", 1)[1]
            foto_bytes = base64.b64decode(foto_b64)

    if not titulo:
        return jsonify({"error": "Se requiere el campo 'titulo'"}), 400

    # Si no hay foto, generar imagen solo-texto
    if not foto_bytes:
        img_sin_foto = generar_imagen_sin_foto(titulo)
        buf = io.BytesIO()
        img_sin_foto.save(buf, "JPEG", quality=92)
        imagen_bytes = buf.getvalue()
    else:
        try:
            imagen_bytes = generar_imagen(foto_bytes, titulo)
        except Exception:
            # Fallback: siempre devolver imagen con diseño aunque falle la foto
            img_sin_foto = generar_imagen_sin_foto(titulo)
            buf = io.BytesIO()
            img_sin_foto.save(buf, "JPEG", quality=92)
            imagen_bytes = buf.getvalue()

    # FIX 1: POST devuelve imagen binaria directamente (igual que GET)
    # Cloudinary sube en background thread — no bloquea la respuesta a Make
    titulo_hash = hashlib.md5(titulo.encode()).hexdigest()[:12]

    def _upload_bg(img_bytes, pub_id):
        try:
            cloudinary.uploader.upload(
                io.BytesIO(img_bytes),
                public_id=pub_id,
                overwrite=True,
                resource_type="image",
                timeout=60
            )
        except Exception as e:
            app.logger.error(f"Cloudinary bg upload (POST) failed: {str(e)[:100]}")

    t = threading.Thread(
        target=_upload_bg,
        args=(imagen_bytes, f"lared/noticia_{titulo_hash}"),
        daemon=True
    )
    t.start()

    return send_file(
        io.BytesIO(imagen_bytes),
        mimetype='image/jpeg',
        as_attachment=False,
        download_name='lared_noticia.jpg'
    )


# ---------------------------------------------------------------------------
# Caché de media (foto) para evitar N llamadas a WP por artículo
# { media_id: (foto_url, timestamp) }
# ---------------------------------------------------------------------------
_media_cache = {}
_MEDIA_CACHE_TTL = 300  # 5 minutos

# Mapa de IDs de categoría → nombre legible
_CAT_NAMES = {
    1:     "Sin Categoria",
    4:     "Internacionales",
    8:     "Nacionales",
    6879:  "Economía",
    34068: "Futbol Nacional",
    36690: "Futbol Internacional",
    36710: "Deporte nacional",
    36775: "Deporte internacional",
}

WP_API = "https://cms.lared1061.com/wp-json/wp/v2"
WP_TIMEOUT = 12

# Sucuri bloquea IPs de datacenter — headers completos de browser para bypasear
WP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-GT,es;q=0.9,en;q=0.8",
    "Referer": "https://www.lared1061.com/",
    "Origin": "https://www.lared1061.com",
    "sec-fetch-site": "cross-site",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}


def _get_media_url(media_id):
    """Devuelve la URL de la imagen del post. Cachea 5 min."""
    if not media_id:
        return ""
    now = time.time()
    if media_id in _media_cache:
        url, ts = _media_cache[media_id]
        if now - ts < _MEDIA_CACHE_TTL:
            return url
    try:
        r = requests.get(
            f"{WP_API}/media/{media_id}",
            params={"_fields": "source_url"},
            headers=WP_HEADERS,
            timeout=WP_TIMEOUT
        )
        url = r.json().get("source_url", "") if r.ok else ""
    except Exception:
        url = ""
    _media_cache[media_id] = (url, now)
    return url


def _wp_date_to_rfc2822(date_str):
    """Convierte '2026-05-04T14:30:00' (date_gmt de WP, ya en UTC) → RFC 2822 GMT."""
    try:
        dt = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        return formatdate(dt.timestamp(), usegmt=True)
    except Exception:
        return formatdate(usegmt=True)


# Desfase de zona horaria de Guatemala respecto a GMT (UTC-6).
# El feed de Vercel etiqueta las fechas locales (GT) como si fueran GMT,
# atrasando cada nota 6h. Sumamos 6h para corregir a GMT real.
_GT_TO_GMT_SECONDS = 6 * 3600


def _fix_vercel_tz(xml_bytes):
    """
    Corrige el desfase de 6h del feed de Vercel.

    Vercel toma la fecha LOCAL de WordPress (hora Guatemala, campo `date`) y la
    etiqueta como GMT -> cada <pubDate> queda 6h atrasado. Para Make eso hace que
    cada nota nueva nazca "vieja" (por debajo del puntero del trigger) y nunca se
    publique. Aqui parseamos cada <pubDate>, le sumamos 6h y re-serializamos.

    Solo corrige fechas que NO traen offset explicito distinto de +0000/GMT
    (si algun dia Vercel arregla el feed y manda offset real, no lo tocamos).
    """
    import re as _re
    try:
        text = xml_bytes.decode("utf-8", "ignore")
    except Exception:
        return xml_bytes

    def _bump(m):
        raw = m.group(1).strip()
        try:
            dt = parsedate_to_datetime(raw)
        except Exception:
            return m.group(0)
        # parsedate_to_datetime de "... GMT" o "... +0000" da tz=UTC.
        # Si el feed YA trae un offset real distinto de 0, asumimos que esta
        # bien y no lo tocamos.
        off = dt.utcoffset()
        if off is not None and off.total_seconds() != 0:
            return m.group(0)
        fixed = formatdate(dt.timestamp() + _GT_TO_GMT_SECONDS, usegmt=True)
        return f"<pubDate>{fixed}</pubDate>"

    fixed_text = _re.sub(r"<pubDate>(.*?)</pubDate>", _bump, text, flags=_re.S)
    return fixed_text.encode("utf-8")


def _escape_xml(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# Cache global del último feed exitoso (sobrevive cold starts en memoria)
_feed_cache = {"xml": None, "ts": 0, "source": None}
_FEED_CACHE_TTL = 3600  # 1 hora máximo

# Cache de fechas REALES por GUID (URL de la nota). Lo llenan WP/Vercel y lo usa
# el scraper de emergencia para NO inventar fechas y no confundir a Make.
# PERSISTIDO en disco para que los multiples workers de gunicorn compartan las
# mismas fechas (si no, cada worker inventa una fecha distinta y desincroniza Make).
import json as _json
_REAL_DATES_FILE = "/tmp/lared_real_dates.json"
_real_dates_lock = threading.Lock()

def _load_real_dates():
    try:
        with open(_REAL_DATES_FILE, "r") as f:
            return _json.load(f)
    except Exception:
        return {}

def _save_real_dates(d):
    try:
        tmp = _REAL_DATES_FILE + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(d, f)
        os.replace(tmp, _REAL_DATES_FILE)
    except Exception:
        pass

_real_dates = _load_real_dates()  # { guid_url: pubDate_rfc2822 }, compartido en disco

VERCEL_RSS = "https://www.lared1061.com/feed"
VERCEL_TIMEOUT = 12
NEXTJS_URL = "https://www.lared1061.com"  # Capa 1.5: scraper del sitio Next.js


def _build_feed_from_nextjs():
    """
    Capa 1.5: scraper del HTML de www.lared1061.com (Next.js App Router).
    No usa __NEXT_DATA__ (no existe en App Router). Extrae posts via regex:
      - <a href="/posts/SLUG"><h3>TITLE</h3> para slug+título
      - <img alt="TITLE" srcSet="/_next/image?url=ENCODED_URL"> para imagen
    Funciona desde cualquier IP — no pasa por Sucuri WP.
    """
    import re as _re
    from urllib.parse import unquote as _unquote

    r = requests.get(
        NEXTJS_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-GT,es;q=0.9",
        },
        timeout=VERCEL_TIMEOUT
    )
    if not r.ok:
        raise ValueError(f"Next.js home {r.status_code}")

    html = r.text

    # Mapa alt_text -> url_imagen desde srcSet de Next.js
    # Formato: /_next/image?url=https%3A%2F%2Fcms...&w=32&q=75 (puede tener &amp; en HTML)
    img_map = {}
    for alt, srcset in _re.findall(r'<img[^>]+alt="([^"]+)"[^>]+srcSet="([^"]+)"', html):
        m = _re.search(r'url=(https?(?:%3A|:)[^&"\s]+)', srcset)
        if m:
            img_map[alt] = _unquote(m.group(1))

    # Extraer posts: <a href="/posts/SLUG"><h3...>TITLE</h3>
    raw_posts = _re.findall(
        r'<a[^>]+href="(/posts/([^"]+))"[^>]*>\s*<h3[^>]*>([^<]+)</h3>',
        html
    )

    if not raw_posts:
        raise ValueError("No se encontraron posts en el HTML de Next.js")

    # Deduplicar por slug manteniendo orden
    seen_slugs = set()
    items = []
    # El home Next.js NO expone la fecha real de cada nota. Para NO confundir el
    # puntero de Make, NO inventamos fechas "ahora-60s" (eso rejuvenece notas
    # viejas). En su lugar reusamos la fecha REAL cacheada por GUID (la llenan
    # WP/Vercel). Si una nota no tiene fecha cacheada (primera vez que la vemos),
    # le damos una base decreciente desde "ahora" SOLO como ultimo recurso, para
    # preservar el orden cronologico del home. Esta capa es de emergencia.
    _now_ts = time.time()
    _pos = 0
    for path, slug, title_raw in raw_posts[:40]:
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        if len(items) >= 20:
            break

        title = _escape_xml(title_raw.strip())
        pub_link = _escape_xml(f"https://www.lared1061.com{path}")
        real_link = f"https://www.lared1061.com{path}"
        with _real_dates_lock:
            # Recargar del disco para ver fechas que otro worker ya fijo.
            disk = _load_real_dates()
            if disk:
                _real_dates.update(disk)
            cached_date = _real_dates.get(real_link)
            if cached_date:
                # Fecha REAL/ya-fijada conocida -> usarla siempre (estable).
                pub_date = cached_date
            else:
                # Nota SIN fecha real conocida (Vercel aun no la trae). NUNCA le
                # damos "ahora": una fecha futura/reciente sintetica envenena el
                # puntero de Make (la veria como "la mas nueva" y al cambiar luego
                # desincroniza). En su lugar le damos una fecha VIEJA estable
                # (epoch base - pos), asi queda al fondo del feed y no se trata
                # como novedad hasta que Vercel publique su fecha REAL (entonces
                # se cachea y sube al lugar correcto). Persistimos por GUID.
                _OLD_BASE = 1700000000  # 2023, claramente vieja
                pub_date = formatdate(_OLD_BASE - (_pos * 60), usegmt=True)
                _real_dates[real_link] = pub_date
                _save_real_dates(_real_dates)
        _pos += 1

        foto_url = img_map.get(title_raw.strip(), "")
        media_xml = (
            f'<media:content url="{_escape_xml(foto_url)}" type="image/jpeg" medium="image" />'
            if foto_url else ""
        )

        try:
            _sort_ts = parsedate_to_datetime(pub_date).timestamp()
        except Exception:
            _sort_ts = _now_ts - (_pos * 60)

        items.append((_sort_ts, f"""    <item>
      <title>{title}</title>
      <link>{pub_link}</link>
      <guid isPermaLink="true">{pub_link}</guid>
      <pubDate>{pub_date}</pubDate>
      {media_xml}
    </item>"""))

    if not items:
        raise ValueError("Lista de items vacía tras parsear Next.js")

    # Ordenar descendente por fecha real: la nota más reciente SIEMPRE arriba,
    # así Make detecta novedades correctamente (procesa el feed de arriba abajo).
    items.sort(key=lambda t: t[0], reverse=True)
    items = [xml for _, xml in items]

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>La Red 106.1 - Noticias</title>
    <link>https://www.lared1061.com</link>
    <description>Noticias de Guatemala y el Mundo</description>
    <language>es-GT</language>
    <lastBuildDate>{formatdate(usegmt=True)}</lastBuildDate>
{"".join(items)}
  </channel>
</rss>"""
    return feed.encode("utf-8")


def _build_feed_from_wp():
    """Construye RSS desde WP REST API directa. Último recurso."""
    r = requests.get(
        f"{WP_API}/posts",
        params={"per_page": 20, "_fields": "id,title,date_gmt,link,categories,featured_media", "status": "publish"},
        headers=WP_HEADERS,
        timeout=WP_TIMEOUT
    )
    if not r.ok:
        raise ValueError(f"WP REST API {r.status_code}")
    posts = r.json()
    items = []
    for p in posts:
        title = _escape_xml(p.get("title", {}).get("rendered", ""))
        slug = p.get("link", "").rstrip("/").split("/")[-1]
        pub_link = f"https://www.lared1061.com/posts/{slug}"
        pub_date = _wp_date_to_rfc2822(p.get("date_gmt", ""))
        cat_names = [_CAT_NAMES.get(cid, str(cid)) for cid in p.get("categories", [])]
        categories_xml = "".join(f"<category>{_escape_xml(c)}</category>" for c in cat_names)
        media_id = p.get("featured_media") or 0
        foto_url = _get_media_url(media_id) if media_id else ""
        media_xml = (f'<media:content url="{_escape_xml(foto_url)}" type="image/jpeg" medium="image" />' if foto_url else "")
        items.append(f"""    <item>
      <title>{title}</title>
      <link>{_escape_xml(pub_link)}</link>
      <guid isPermaLink="true">{_escape_xml(pub_link)}</guid>
      <pubDate>{pub_date}</pubDate>
      {categories_xml}
      {media_xml}
    </item>""")
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>La Red 106.1 - Noticias</title>
    <link>https://www.lared1061.com</link>
    <description>Noticias de Guatemala y el Mundo</description>
    <language>es-GT</language>
    <lastBuildDate>{formatdate(usegmt=True)}</lastBuildDate>
{"".join(items)}
  </channel>
</rss>"""
    return feed.encode("utf-8")


def _vercel_feed_is_fresh(xml_bytes):
    """
    Devuelve True si el RSS de Vercel esta al dia comparado con el home Next.js.
    El feed de Vercel a veces se queda CACHEADO/ATASCADO y no incluye las notas
    recien publicadas (mientras el home si las muestra). En ese caso Make no ve
    novedades y deja de publicar. Comparamos el slug de la nota #0 del home con
    los slugs del feed: si la mas reciente del home NO esta en el feed, esta viejo.
    """
    import re as _re
    try:
        r = requests.get(
            NEXTJS_URL,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
            timeout=VERCEL_TIMEOUT,
        )
        if not r.ok:
            return True  # no podemos comparar -> asumimos fresco (no bloquear Vercel)
        posts = _re.findall(r'<a[^>]+href="/posts/([^"]+)"[^>]*>\s*<h3', r.text)
        if not posts:
            return True
        # slugs mas recientes del home (top 10), deduplicados manteniendo orden
        top_home = []
        seen = set()
        for s in posts:
            if s in seen:
                continue
            seen.add(s)
            top_home.append(s)
            if len(top_home) >= 10:
                break
        feed_slugs = set(_re.findall(r'/posts/([^<]+)</link>', xml_bytes.decode("utf-8", "ignore")))
        # Contamos cuantas de las top-10 del home FALTAN en el feed.
        # El home mezcla notas nuevas con destacados fijos (que si estan en el feed),
        # asi que un umbral suave: si faltan 3+ de las 10 mas recientes, el feed de
        # Vercel se quedo atascado y no trae las notas nuevas -> usar scraper.
        faltantes = sum(1 for s in top_home if s not in feed_slugs)
        if faltantes >= 6:
            app.logger.warning(f"Vercel RSS desactualizado: faltan {faltantes}/10 notas recientes del home")
            return False
        return True
    except Exception as e:
        app.logger.warning(f"chequeo frescura Vercel fallo: {type(e).__name__}: {e}")
        return True


@app.route("/rss-proxy", methods=["GET"])
def rss_proxy():
    """
    RSS proxy con fechas REALES y coherentes para que el puntero interno del
    trigger de Make NO se desincronice y deje notas sin publicar.

    Nota: la WP REST API esta bloqueada por el WAF de SiteGround/Sucuri desde
    IPs de datacenter (Render), asi que NO se usa como primaria en prod. Vercel
    SI es accesible y trae fechas reales. Orden de capas:

    1. RSS de Vercel        -> PRIMARIA si esta fresco. Fechas reales, GUID estable.
    2. Scraper Next.js      -> si Vercel esta atascado. Reusa fechas reales
                               cacheadas por GUID desde Vercel (NO inventa
                               "ahora-60s" para notas ya conocidas).
    3. Cache en memoria     -> ultimo feed bueno (hasta 1h).
    4. WP REST API directa  -> ultimo recurso (suele fallar por WAF en prod).
    """
    global _feed_cache
    import xml.etree.ElementTree as ET

    # --- Capa 1: RSS Vercel (PRIMARIA, fechas reales, solo si esta fresco) ---
    try:
        resp = requests.get(
            VERCEL_RSS,
            headers={"User-Agent": "Mozilla/5.0 (compatible; RSSProxy/1.0)", "Accept": "application/rss+xml, application/xml"},
            timeout=VERCEL_TIMEOUT
        )
        if resp.ok and len(resp.content) > 500:
            ET.fromstring(resp.content)  # validar XML
            # Corregir el desfase de 6h (Vercel etiqueta hora GT como GMT).
            vercel_xml = _fix_vercel_tz(resp.content)
            _remember_real_dates(vercel_xml)  # cachear fechas YA corregidas
            if _vercel_feed_is_fresh(resp.content):
                _feed_cache["xml"] = vercel_xml
                _feed_cache["ts"] = time.time()
                _feed_cache["source"] = "vercel"
                app.logger.info("rss-proxy: sirviendo desde Vercel RSS (primaria, tz corregida)")
                return Response(vercel_xml, mimetype="application/rss+xml")
            else:
                app.logger.warning("rss-proxy: Vercel RSS atascado, usando scraper Next.js")
    except Exception as e:
        app.logger.warning(f"rss-proxy capa1 (Vercel) falló: {type(e).__name__}: {e}")

    # --- Capa 2: Scraper Next.js (fechas reales cacheadas desde Vercel) ---
    try:
        feed_xml = _build_feed_from_nextjs()
        _feed_cache["xml"] = feed_xml
        _feed_cache["ts"] = time.time()
        _feed_cache["source"] = "nextjs"
        app.logger.info("rss-proxy: sirviendo desde Next.js scraper")
        return Response(feed_xml, mimetype="application/rss+xml")
    except Exception as e:
        app.logger.warning(f"rss-proxy capa2 (Next.js scraper) falló: {type(e).__name__}: {e}")

    # --- Capa 3: Cache en memoria ---
    if _feed_cache["xml"] and (time.time() - _feed_cache["ts"]) < _FEED_CACHE_TTL:
        app.logger.info("rss-proxy: sirviendo desde cache")
        return Response(_feed_cache["xml"], mimetype="application/rss+xml")

    # --- Capa 4: WP REST API directa (ultimo recurso) ---
    try:
        feed_xml = _build_feed_from_wp()
        ET.fromstring(feed_xml)
        _remember_real_dates(feed_xml)
        _feed_cache["xml"] = feed_xml
        _feed_cache["ts"] = time.time()
        _feed_cache["source"] = "wp"
        app.logger.info("rss-proxy: sirviendo desde WP REST API (ultimo recurso)")
        return Response(feed_xml, mimetype="application/rss+xml")
    except Exception as e:
        app.logger.error(f"rss-proxy capa4 (WP REST API) falló: {e}")

    return Response("Feed temporalmente no disponible", status=503, mimetype="text/plain")


def _remember_real_dates(xml_bytes):
    """Guarda las fechas reales (pubDate) indexadas por GUID/link.
    Las fechas REALES de Vercel/WP sobreescriben cualquier fecha sintetica
    previa (asi una nota que era nueva adopta su fecha real cuando aparece)."""
    import re as _re
    try:
        txt = xml_bytes.decode("utf-8", "ignore") if isinstance(xml_bytes, bytes) else xml_bytes
        with _real_dates_lock:
            disk = _load_real_dates()
            if disk:
                _real_dates.update(disk)
            changed = False
            for item in _re.findall(r"<item>.*?</item>", txt, _re.DOTALL):
                link_m = _re.search(r"<link>([^<]+)</link>", item)
                date_m = _re.search(r"<pubDate>([^<]+)</pubDate>", item)
                if link_m and date_m:
                    _real_dates[link_m.group(1).strip()] = date_m.group(1).strip()
                    changed = True
            if changed:
                _save_real_dates(_real_dates)
    except Exception:
        pass


@app.route("/rss-health", methods=["GET"])
def rss_health():
    """Salud del feed: fuente usada, nº items, fecha de la nota mas nueva y
    si hay desfase entre WP y Vercel. Para monitorear sin adivinar."""
    import xml.etree.ElementTree as ET
    out = {"source": None, "items": 0, "newest": None, "wp_ok": False,
           "vercel_ok": False, "vercel_fresh": None, "desfase": None}
    # WP
    try:
        wp_xml = _build_feed_from_wp()
        root = ET.fromstring(wp_xml)
        items = root.findall(".//item")
        out["wp_ok"] = True
        out["items"] = len(items)
        if items:
            pd = items[0].find("pubDate")
            out["newest"] = pd.text if pd is not None else None
        wp_links = {it.find("link").text for it in items if it.find("link") is not None}
    except Exception as e:
        wp_links = set()
        out["wp_error"] = f"{type(e).__name__}: {e}"
    # Vercel
    try:
        resp = requests.get(VERCEL_RSS, headers={"User-Agent": "Mozilla/5.0"}, timeout=VERCEL_TIMEOUT)
        if resp.ok and len(resp.content) > 500:
            out["vercel_ok"] = True
            out["vercel_fresh"] = _vercel_feed_is_fresh(resp.content)
            import re as _re
            v_links = set(_re.findall(r"<link>([^<]+)</link>", resp.content.decode("utf-8", "ignore")))
            if wp_links:
                out["desfase"] = len([l for l in wp_links if l not in v_links])
    except Exception as e:
        out["vercel_error"] = f"{type(e).__name__}: {e}"
    out["source"] = _feed_cache.get("source")
    return jsonify(out)



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
