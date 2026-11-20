"""
Componentes visuais reutilizáveis do Telegram Cloud Drive:
- Gerador de ícones vetoriais supersampled 4x (_create_nav_icons)
- Wallpaper oficial estilizado do Telegram Night Theme (draw_telegram_wallpaper_canvas)
- Barra de progresso customizada com porcentagem e ETA (LabeledProgressBar)
"""

import math
import tkinter as tk
from typing import Dict, Optional
import customtkinter as ctk
from PIL import Image, ImageDraw


def _create_nav_icons() -> Dict[str, ctk.CTkImage]:
    size = 128  # Desenha em 128x128 e reduz para 28x28 com LANCZOS para bordas perfeitas
    display_size = (28, 28)

    # 1. Ícone UPLOAD: Setinha Verde para Cima em badge arredondado escuro/verde
    img_up = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_up = ImageDraw.Draw(img_up)
    d_up.rounded_rectangle(
        (6, 6, size - 6, size - 6),
        radius=26,
        fill=(22, 163, 74, 255),
        outline=(134, 239, 172, 255),
        width=6,
    )
    d_up.polygon(
        [(64, 22), (26, 64), (47, 64), (47, 102), (81, 102), (81, 64), (102, 64)],
        fill=(255, 255, 255, 255),
    )
    img_up_small = img_up.resize(display_size, Image.Resampling.LANCZOS)

    # 2. Ícone DOWNLOAD: Setinha Azul para Baixo em badge arredondado azul
    img_dl = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_dl = ImageDraw.Draw(img_dl)
    d_dl.rounded_rectangle(
        (6, 6, size - 6, size - 6),
        radius=26,
        fill=(2, 132, 199, 255),
        outline=(125, 211, 252, 255),
        width=6,
    )
    d_dl.polygon(
        [(64, 106), (26, 64), (47, 64), (47, 26), (81, 26), (81, 64), (102, 64)],
        fill=(255, 255, 255, 255),
    )
    img_dl_small = img_dl.resize(display_size, Image.Resampling.LANCZOS)

    # 3. Ícone RELÓGIO: Histórico de Enviados
    img_clk = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_clk = ImageDraw.Draw(img_clk)
    d_clk.ellipse(
        (8, 8, size - 8, size - 8),
        fill=(217, 119, 6, 255),
        outline=(253, 224, 71, 255),
        width=8,
    )
    for angle_deg in range(0, 360, 90):
        rad = math.radians(angle_deg)
        x1 = 64 + int(38 * math.cos(rad))
        y1 = 64 + int(38 * math.sin(rad))
        x2 = 64 + int(44 * math.cos(rad))
        y2 = 64 + int(44 * math.sin(rad))
        d_clk.line([(x1, y1), (x2, y2)], fill=(255, 255, 255, 255), width=6)
    d_clk.line([(64, 64), (64, 32)], fill=(255, 255, 255, 255), width=10)
    d_clk.line([(64, 64), (90, 76)], fill=(255, 255, 255, 255), width=9)
    d_clk.ellipse((56, 56, 72, 72), fill=(255, 255, 255, 255))
    img_clk_small = img_clk.resize(display_size, Image.Resampling.LANCZOS)

    # 4. Ícone TELEGRAM: Círculo Azul Telegram + Aviãozinho de Papel Branco
    img_tg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_tg = ImageDraw.Draw(img_tg)
    d_tg.ellipse(
        (6, 6, size - 6, size - 6),
        fill=(36, 161, 222, 255),
        outline=(186, 230, 253, 255),
        width=6,
    )
    d_tg.polygon(
        [(22, 64), (100, 30), (84, 98), (58, 78), (44, 92), (46, 70)],
        fill=(255, 255, 255, 255),
    )
    d_tg.polygon(
        [(46, 70), (88, 38), (58, 78)],
        fill=(219, 234, 254, 255),
    )
    img_tg_small = img_tg.resize(display_size, Image.Resampling.LANCZOS)

    # 5. Ícone PLAY/PAUSE combinado
    btn_icon_size = (22, 22)
    img_pp = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_pp = ImageDraw.Draw(img_pp)
    d_pp.polygon([(14, 24), (14, 104), (66, 64)], fill=(255, 255, 255, 255))
    d_pp.rounded_rectangle((78, 24, 94, 104), radius=4, fill=(255, 255, 255, 255))
    d_pp.rounded_rectangle((104, 24, 120, 104), radius=4, fill=(255, 255, 255, 255))
    img_pp_small = img_pp.resize(btn_icon_size, Image.Resampling.LANCZOS)

    # 6. Ícone PAUSE
    img_pause = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_pause = ImageDraw.Draw(img_pause)
    d_pause.rounded_rectangle((22, 20, 48, 108), radius=6, fill=(255, 255, 255, 255))
    d_pause.rounded_rectangle((62, 20, 88, 108), radius=6, fill=(255, 255, 255, 255))
    d_pause.polygon([(98, 40), (98, 88), (122, 64)], fill=(255, 255, 255, 220))
    img_pause_small = img_pause.resize(btn_icon_size, Image.Resampling.LANCZOS)

    # 7. Ícone PLAY
    img_play = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_play = ImageDraw.Draw(img_play)
    d_play.polygon([(24, 16), (24, 112), (110, 64)], fill=(255, 255, 255, 255))
    img_play_small = img_play.resize(btn_icon_size, Image.Resampling.LANCZOS)

    return {
        "upload": ctk.CTkImage(light_image=img_up_small, dark_image=img_up_small, size=display_size),
        "download": ctk.CTkImage(light_image=img_dl_small, dark_image=img_dl_small, size=display_size),
        "history": ctk.CTkImage(light_image=img_clk_small, dark_image=img_clk_small, size=display_size),
        "account": ctk.CTkImage(light_image=img_tg_small, dark_image=img_tg_small, size=display_size),
        "play_pause": ctk.CTkImage(light_image=img_pp_small, dark_image=img_pp_small, size=btn_icon_size),
        "pause": ctk.CTkImage(light_image=img_pause_small, dark_image=img_pause_small, size=btn_icon_size),
        "play": ctk.CTkImage(light_image=img_play_small, dark_image=img_play_small, size=btn_icon_size),
    }


def draw_telegram_wallpaper_canvas(canvas: tk.Canvas):
    """Desenha o background oficial estilizado do Telegram (padrão geométrico + aviãozinho central) no Canvas."""
    canvas.delete("tg_bg")
    w = max(canvas.winfo_width(), 200)
    h = max(canvas.winfo_height(), 120)

    canvas.configure(bg="#0e1621")

    pattern_color = "#18283a"
    accent_pattern = "#1c3148"
    step_x = 130
    step_y = 95

    for row_idx, y in enumerate(range(25, h + 60, step_y)):
        offset_x = 65 if (row_idx % 2 == 1) else 0
        for col_idx, x in enumerate(range(30 + offset_x, w + 60, step_x)):
            kind = (row_idx + col_idx) % 4
            if kind == 0:
                canvas.create_polygon(
                    x - 14, y + 2,
                    x + 14, y - 10,
                    x + 8, y + 14,
                    x - 2, y + 6,
                    outline=pattern_color,
                    fill="",
                    width=1.5,
                    tags="tg_bg",
                )
            elif kind == 1:
                canvas.create_oval(x - 14, y - 4, x + 2, y + 10, outline=accent_pattern, width=1.5, tags="tg_bg")
                canvas.create_oval(x - 6, y - 10, x + 12, y + 8, outline=accent_pattern, width=1.5, tags="tg_bg")
                canvas.create_line(x - 10, y + 10, x + 10, y + 10, fill=accent_pattern, width=1.5, tags="tg_bg")
            elif kind == 2:
                canvas.create_polygon(
                    x - 12, y - 8,
                    x - 3, y - 8,
                    x, y - 4,
                    x + 12, y - 4,
                    x + 12, y + 9,
                    x - 12, y + 9,
                    outline=pattern_color,
                    fill="",
                    width=1.5,
                    tags="tg_bg",
                )
            else:
                canvas.create_polygon(
                    x, y - 8,
                    x + 8, y,
                    x, y + 8,
                    x - 8, y,
                    outline=accent_pattern,
                    fill="",
                    width=1.5,
                    tags="tg_bg",
                )

    cx = w // 2
    cy = h // 2
    r = min(max(int(min(w, h) * 0.20), 34), 68)

    if h >= 90:
        canvas.create_oval(
            cx - r - 10,
            cy - r - 18,
            cx + r + 10,
            cy + r + 2,
            fill="#132334",
            outline="#1e3a56",
            width=2,
            tags="tg_bg",
        )
        canvas.create_oval(
            cx - r,
            cy - r - 8,
            cx + r,
            cy + r - 8,
            fill="#1d8ac0",
            outline="#38bdf8",
            width=2,
            tags="tg_bg",
        )
        cy_plane = cy - 8
        canvas.create_polygon(
            cx - int(r * 0.62), cy_plane + int(r * 0.02),
            cx + int(r * 0.54), cy_plane - int(r * 0.46),
            cx + int(r * 0.32), cy_plane + int(r * 0.52),
            cx - int(r * 0.08), cy_plane + int(r * 0.22),
            cx - int(r * 0.28), cy_plane + int(r * 0.42),
            cx - int(r * 0.25), cy_plane + int(r * 0.10),
            fill="#e0f2fe",
            outline="",
            tags="tg_bg",
        )
        canvas.create_polygon(
            cx - int(r * 0.25), cy_plane + int(r * 0.10),
            cx + int(r * 0.38), cy_plane - int(r * 0.34),
            cx - int(r * 0.08), cy_plane + int(r * 0.22),
            fill="#bae6fd",
            outline="",
            tags="tg_bg",
        )
        if h >= 170:
            canvas.create_text(
                cx,
                cy + r + 16,
                text="TELEGRAM CLOUD DRIVE",
                fill="#38bdf8",
                font=("Segoe UI", 11, "bold"),
                tags="tg_bg",
            )


class LabeledProgressBar(tk.Canvas):
    """Barra de progresso estilizada com contador percentual e tempo previsto centralizados dentro da barra."""

    def __init__(
        self,
        master,
        height: int = 20,
        progress_color: str = "#2563eb",
        bg_color: str = "#0f172a",
        border_color: str = "#334155",
        **kwargs,
    ):
        super().__init__(
            master,
            height=height,
            bg=bg_color,
            highlightthickness=1,
            highlightbackground=border_color,
            bd=0,
            **kwargs,
        )
        self._progress_color = progress_color
        self._bg_color = bg_color
        self._value = 0.0
        self._custom_text: Optional[str] = None
        self.bind("<Configure>", lambda _e: self._redraw())

    def set(self, value: float, custom_text: Optional[str] = None):
        try:
            v = float(value)
        except Exception:
            v = 0.0
        self._value = max(0.0, min(1.0, v))
        self._custom_text = custom_text
        self._redraw()

    def get(self) -> float:
        return self._value

    def _redraw(self):
        try:
            self.delete("all")
            w = max(self.winfo_width(), 100)
            h = max(self.winfo_height(), 18)
            fill_w = int(round(w * self._value))
            if fill_w > 0:
                self.create_rectangle(
                    0, 0, fill_w, h, fill=self._progress_color, outline=""
                )
            display_str = (
                self._custom_text
                if self._custom_text is not None
                else f"{self._value * 100:0.1f}%"
            )
            cx = w // 2
            cy = h // 2
            self.create_text(
                cx + 1,
                cy + 1,
                text=display_str,
                fill="#020617",
                font=("Segoe UI", 9, "bold"),
            )
            self.create_text(
                cx,
                cy,
                text=display_str,
                fill="#ffffff",
                font=("Segoe UI", 9, "bold"),
            )
        except Exception:
            pass
