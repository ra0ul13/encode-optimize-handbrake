crf_optimizer.py — Mesure précise des bitrates vidéo/audio d'une source Blu-ray
(dossier BDMV) ou d'un M2TS, puis calcule le CRF optimal x264 pour atteindre
un % cible du bitrate vidéo.

En mode disque, HandBrakeCLI encode via la playlist (--main-feature ou --title N),
ce qui préserve chapitres, langues des pistes audio / sous-titres, etc.

Dépendances : ffmpeg, ffprobe (sudo apt install ffmpeg), HandBrakeCLI

Usage :
  ./crf_optimizer.py /chemin/DISQUE                 (dossier contenant BDMV)
  ./crf_optimizer.py /chemin/DISQUE/BDMV/STREAM/00800.m2ts
  ./crf_optimizer.py /chemin/DISQUE --title 3       (force un titre HandBrake)
  ./crf_optimizer.py film.m2ts --no-disc            (ancien mode : m2ts seul)
  ./crf_optimizer.py /chemin/DISQUE --calibrate --segments 8 --encode handbrake
