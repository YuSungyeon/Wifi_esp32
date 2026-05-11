# Design System for Premium Consulting-Style PPT (16:9)

## 0. Production Constraints (Read First)

Output
- 16:9 slides only (PowerPoint standard 13.333" × 7.5", reference resolution 1920 × 1080 px). No other aspect ratios are valid output.
- All deliverables should use the provided master template if one exists.

Brand Assets (mandatory, no substitution)
- Logo: Use only the client/project logo asset provided as `icon.svg`.
- Place the logo at the top-right of every slide unless explicitly instructed otherwise.
- Typography: Pretendard only — no exceptions. Do NOT use DM Sans, Outfit, Poppins, Roboto, Noto, system fallbacks, or any other family under any circumstance.
- Every weight reference below maps to Pretendard's scale (Thin 100 → Black 900).
- Assume Pretendard is available on the rendering machine.
- If a fallback string is needed for export, use:
  Pretendard, "Pretendard Variable", -apple-system, system-ui, sans-serif
- The only family that should actually render is Pretendard.

---

# Slide Skeleton — locked positions across the deck

Every slide in the deck must place these five zones at identical coordinates.
The reader's eye should never have to relearn the layout when flipping pages — only the body contents change, never the frame.

| Zone | Position (slide 13.333" × 7.5") | Contents | Style |
|---|---|---|---|
| Header strip | 0.4"–0.7" from top, full width within 0.5" side margins | Chapter name (left), logo (right) | Chapter: Pretendard 600, 12pt, #8e8e93 |
| Headline zone | 1.0"–1.75" from top, 0.5" left margin | Slide headline | Pretendard 700, 32–40pt, #222222 |
| Subtitle zone | 1.63"–2.03" from top, 0.5" left margin | Subtitle | Pretendard 500, 16pt, #45515e |
| Body box | 2.39"–6.85" from top, 0.5" side margins | All body components | Mixed |
| Footer strip | 7.05"–7.3" from top | Page number / source | Pretendard 400–500 |

---

## Logo Integrity Rule

The logo must be placed exactly as provided in `icon.svg`.

Allowed operations:
- Uniform scaling while preserving aspect ratio
- Uniform white inversion for dark backgrounds

Forbidden:
- Underlines
- Shadows
- Glow
- Borders
- Frames
- Recoloring
- Gradients
- Opacity changes
- Background boxes
- Cropping
- Stretching
- Rotation
- Duplication

If a visible rectangle/background appears behind the logo, treat it as a rendering defect and fix it before export.

---

# Body Density Rule

The lower body box must NOT be left visually empty.

Use:
- Supporting evidence panels
- KPI cards
- Charts
- Diagrams
- 2-column layouts
- "So What" summary boxes
- Stacked insight layers

Never:
- Use decorative filler
- Add meaningless stock illustrations
- Spill content into footer zones

---

# Visualization-First Rule

Whenever a slide contains:
- comparison
- trend
- distribution
- process
- hierarchy
- structure
- relationship
- sequence
- geographic breakdown

...the information MUST be visualized rather than explained only through prose.

Preferred order:
1. Charts
2. KPI tiles
3. Diagrams
4. Annotated screenshots
5. Tables (last resort)

---

# 1. Visual Theme & Atmosphere

The aesthetic bridges:
- premium product-marketing clarity
- modern consulting presentation structure
- soft rounded gallery-like UI language

Core characteristics:
- White-dominant canvas
- Color used mainly in charts/KPI cards/highlights
- Pretendard weight-based hierarchy
- Rounded cards
- Pill buttons
- Airy spacing
- Soft elevation shadows
- Occasional dark divider slides

---

# 2. Color Palette & Roles

## Primary Palette
- Primary Blue (#1456f0)
- Accent Blue (#3daeff)
- Accent Pink (#ea5ec1)

## Blue Scale
- #bfdbfe
- #60a5fa
- #3b82f6
- #2563eb
- #1d4ed8
- #17437d

## Text
- #222222
- #18181b
- #181e25
- #45515e
- #8e8e93
- #5f5f5f

## Surface
- #ffffff
- #f0f0f0
- #f2f3f5
- #e5e7eb

---

# Shadow Library

| Token | Value | Use |
|---|---|---|
| Standard | rgba(0,0,0,0.08) 0px 4px 6px | Standard cards |
| Soft Glow | rgba(0,0,0,0.08) 0px 0px 22px | Ambient glow |
| Accent Glow | rgba(44,30,116,0.16) 0px 0px 15px | Featured elements |
| Elevated | rgba(36,36,36,0.08) 0px 12px 16px -4px | Hero emphasis |

---

# Hero Gradient

```css
linear-gradient(
  135deg,
  #1456f0 0%,
  #3b82f6 50%,
  #60a5fa 100%
)
