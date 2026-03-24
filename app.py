"""
API de generación de imágenes para La Red 106.1
Endpoint: POST /generar-imagen
Body (application/json o form-data):
  - foto_url: URL de la imagen del artículo (la API la descarga)
  - titulo: texto del título de la noticia
Respuesta: { "imagen_url": "https://res.cloudinary.com/..." }
"""

from flask import Flask, request, jsonify
from PIL import Image, ImageDraw, ImageFont
import requests
import io
import cloudinary
import cloudinary.uploader
import os
import hashlib
import base64

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

# Headers de navegador para evitar bloqueos de captcha
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Accept-Language": "es-GT,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://lared1061.com/",
    "Connection": "keep-alive",
}


def descargar_imagen_url(url):
    """Descarga una imagen desde una URL usando headers de navegador"""
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    resp.raise_for_status()
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
    - Logo LAR=D 106.1 en pastilla roja
    Devuelve la URL pública de Cloudinary.
    """
    # 1. Cargar y recortar foto de fondo
    foto = Image.open(io.BytesIO(foto_bytes)).convert("RGBA")
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

    # Logo pastilla roja
    LOGO_W, LOGO_H_SIZE, LOGO_R = 380, 92, 46
    lx1 = LOGO_CX - LOGO_W // 2
    ly1 = LOGO_CY - LOGO_H_SIZE // 2
    lx2 = LOGO_CX + LOGO_W // 2
    ly2 = LOGO_CY + LOGO_H_SIZE // 2
    draw.rectangle([(lx1 + LOGO_R, ly1), (lx2 - LOGO_R, ly2)], fill=RED)
    draw.rectangle([(lx1, ly1 + LOGO_R), (lx2, ly2 - LOGO_R)], fill=RED)
    draw.ellipse([(lx1, ly1), (lx1 + 2 * LOGO_R, ly1 + 2 * LOGO_R)], fill=RED)
    draw.ellipse([(lx2 - 2 * LOGO_R, ly1), (lx2, ly1 + 2 * LOGO_R)], fill=RED)
    draw.ellipse([(lx1, ly2 - 2 * LOGO_R), (lx1 + 2 * LOGO_R, ly2)], fill=RED)
    draw.ellipse([(lx2 - 2 * LOGO_R, ly2 - 2 * LOGO_R), (lx2, ly2)], fill=RED)

    # 3. Componer fondo + overlay
    canvas = Image.alpha_composite(fondo, overlay)
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

    # 5. Texto del logo
    try:
        font_logo = ImageFont.truetype(FONT_PATH, 56)
    except OSError:
        font_logo = ImageFont.load_default()
    logo_text = "LAR =D 106.1"
    bbox = draw_final.textbbox((0, 0), logo_text, font=font_logo)
    tw_logo = bbox[2] - bbox[0]
    th_logo = bbox[3] - bbox[1]
    draw_final.text(
        (LOGO_CX - tw_logo // 2, LOGO_CY - th_logo // 2 - 2),
        logo_text,
        fill=(255, 255, 255, 255),
        font=font_logo,
    )

    # 6. Guardar en buffer y subir a Cloudinary
    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, "JPEG", quality=92)
    buffer.seek(0)

    # Usar hash del título como public_id para evitar duplicados
    titulo_hash = hashlib.md5(titulo.encode()).hexdigest()[:12]
    public_id = f"lared/noticia_{titulo_hash}"

    result = cloudinary.uploader.upload(
        buffer,
        public_id=public_id,
        overwrite=True,
        resource_type="image",
    )
    return result["secure_url"]


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
        if foto_url:
            try:
                result = cloudinary.uploader.upload(
                    foto_url,
                    public_id="lared/temp_foto",
                    overwrite=True,
                    resource_type="image"
                )
                cloudinary_url = result["secure_url"]
                resp = requests.get(cloudinary_url, timeout=30)
                resp.raise_for_status()
                foto_bytes = resp.content
            except Exception:
                foto_bytes = descargar_imagen_url(foto_url)
        if not titulo:
            return jsonify({"error": "Se requiere el campo 'titulo'"}), 400
        if not foto_bytes:
            return jsonify({"error": "Se requiere foto_url"}), 400
        try:
            imagen_url = generar_imagen(foto_bytes, titulo)
            return jsonify({"imagen_url": imagen_url, "ok": True})
        except Exception as e:
            return jsonify({"error": str(e), "ok": False}), 500

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
        titulo = data.get("titulo", "").strip()
        foto_url = data.get("foto_url", "").strip()
        foto_b64 = data.get("foto_base64", "").strip()

        if foto_url:
            # Intentar descargar via Cloudinary primero (evita captcha)
            try:
                result = cloudinary.uploader.upload(
                    foto_url,
                    public_id="lared/temp_foto",
                    overwrite=True,
                    resource_type="image"
                )
                cloudinary_url = result["secure_url"]
                resp = requests.get(cloudinary_url, timeout=30)
                resp.raise_for_status()
                foto_bytes = resp.content
            except Exception:
                # Fallback: descarga directa
                foto_bytes = descargar_imagen_url(foto_url)
        elif foto_b64:
            if "," in foto_b64:
                foto_b64 = foto_b64.split(",", 1)[1]
            foto_bytes = base64.b64decode(foto_b64)

    if not titulo:
        return jsonify({"error": "Se requiere el campo 'titulo'"}), 400
    if not foto_bytes:
        return jsonify({"error": "Se requiere la imagen (foto_url, foto_base64, o archivo foto)"}), 400

    try:
        imagen_url = generar_imagen(foto_bytes, titulo)
        return jsonify({"imagen_url": imagen_url, "ok": True})
    except Exception as e:
        return jsonify({"error": str(e), "ok": False}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
