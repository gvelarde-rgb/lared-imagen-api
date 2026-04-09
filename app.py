"""
API de generación de imágenes para La Red 106.1
Endpoint: POST /generar-imagen
Body (application/json o form-data):
  - foto_url: URL de la imagen del artículo (la API la descarga)
  - titulo: texto del título de la noticia
Respuesta: { "imagen_url": "https://res.cloudinary.com/..." }
"""

from flask import Flask, request, jsonify, send_file
from PIL import Image, ImageDraw, ImageFont
import requests
import io
import cloudinary
import cloudinary.uploader
import os
import hashlib
import base64
import threading

app = Flask(__name__)

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
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "es-GT,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://lared1061.com/",
    "Connection": "keep-alive",
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
    Descarga foto_url con fallback via Cloudinary.
    - Vacío → None
    - Descarga directa OK → bytes
    - Captcha/anti-bot (cms.lared1061.com) → Cloudinary fetch como proxy → bytes
    - Todo falla → None (imagen sin foto)
    """
    if not foto_url:
        return None

    # Intento 1: descarga directa
    try:
        return descargar_imagen_url(foto_url)
    except Exception as e1:
        app.logger.warning(f"Descarga directa falló: {str(e1)[:80]}")

    # Fallback: Cloudinary como proxy
    # Sus IPs no están bloqueadas por Sucuri SG Captcha de cms.lared1061.com
    # También cubre: Max retries, captcha, connection refused, etc.
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
    return jsonify({"status": "ok", "servicio": "La Red - Generador de Imágenes"})


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
            except ValueError as e:
                return jsonify({"error": str(e), "ok": False}), 400
            except Exception as e:
                return jsonify({"error": str(e), "ok": False}), 500

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
        except Exception as e:
            return jsonify({"error": str(e), "ok": False}), 500

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


@app.route("/debug-foto", methods=["GET"])
def debug_foto():
    """Endpoint temporal de diagnóstico — ver qué pasa con una URL de foto."""
    url = request.args.get("url", "")
    if not url:
        return jsonify({"error": "falta parametro url"}), 400
    results = {"url": url}
    # Test 1: probe sin redirects
    try:
        probe = requests.get(url, headers=BROWSER_HEADERS, timeout=8, allow_redirects=False)
        results["probe_status"] = probe.status_code
        results["probe_captcha"] = "sg-captcha" in probe.headers
        results["probe_ct"] = probe.headers.get("Content-Type", "")
    except Exception as e:
        results["probe_error"] = str(e)[:100]
    # Test 2: cloudinary upload directo
    try:
        r = cloudinary.uploader.upload(
            url, public_id="lared/debug_test", overwrite=True,
            resource_type="image", timeout=20
        )
        results["cloudinary_ok"] = True
        results["cloudinary_url"] = r["secure_url"]
        results["size_w"] = r.get("width")
        results["size_h"] = r.get("height")
    except Exception as e:
        results["cloudinary_error"] = str(e)[:200]
    return jsonify(results)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
