#!/bin/bash
mkdir -p /home/rs/sound/wav
for file in /home/rs/sound/*.mp3; do
    base=$(basename "$file" .mp3)
    ffmpeg -y -i "$file" -af "adelay=1000|1000" "/home/rs/sound/wav/$base.wav"
done
