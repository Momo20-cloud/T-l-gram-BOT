# Vidéo de présentation

- `presentation-avec-musique.mp4` : version finale avec la bande-son.
- `presentation-sans-son.mp4` : à utiliser avec un son tendance ajouté dans TikTok ou Instagram.

Format : 1080×1920 (vertical), 31 s, 30 images/s, H.264 + AAC.

## La refaire (autre nom, autre durée d'essai…)

1. `promo.html` contient l'animation. Remplace `{{SITE_NAME}}`, `{{TRIAL_DAYS}}` et `{{LINK_LINE}}`.
2. Capture chaque image avec Playwright (`window.render(t)` pour t = 0 → 31 s, à 30 images/s).
3. `python soundtrack.py musique.wav 31` génère la bande-son, composée par programme (libre de droits).
4. Assemble : `ffmpeg -framerate 30 -i f%05d.jpg -i musique.wav -c:v libx264 -crf 18 -pix_fmt yuv420p -c:a aac -shortest video.mp4`
