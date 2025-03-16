# Скрипт для PowerShell который скачивает видео, переводит и сохраняет две аудиодорожки
# Использование:
# .\translate.ps1 <ссылка на видео> [громкость оригинала, например: 0.4] [--keep-original | --replace-audio] [--output-dir <путь>] [--quality <качество>]

function ProcessVideo($video_link, $original_sound_ratio, $keep_original, $output_dir, $quality) {
    $temp_dir = Join-Path $output_dir "temp_$([System.IO.Path]::GetRandomFileName().Split('.')[0])"
    $temp_video_dir = "$temp_dir/video"
    $temp_video = "$temp_video_dir/%(title)s.mp4"
    $temp_audio = "$temp_dir/audio"

    try {
        Write-Host "Creating temp directories: $temp_dir"
        New-Item -ItemType Directory -Path $temp_dir -ErrorAction Stop | Out-Null
        New-Item -ItemType Directory -Path $temp_video_dir -ErrorAction Stop | Out-Null
        New-Item -ItemType Directory -Path $temp_audio -ErrorAction Stop | Out-Null

        Write-Host "Downloading video: $video_link to $temp_video with quality: $quality"
        if ($quality -eq "best") {
            $download_result = yt-dlp -o $temp_video -f "bestvideo+bestaudio/best" $video_link --merge-output-format mp4 --no-progress 2>&1
        } else {
            $height = [int]($quality -replace '[^0-9]', '')
            $download_result = yt-dlp -o $temp_video -f "bestvideo[height<=$height][ext=mp4]+bestaudio[ext=m4a]/best[height<=$height]/best" $video_link --merge-output-format mp4 --no-progress 2>&1
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Error downloading video: $download_result"
            return
        }

        $temp_video_file = (Get-ChildItem -Path $temp_video_dir -ErrorAction SilentlyContinue)[0]
        if (-not $temp_video_file) {
            Write-Host "Error: Video file not found in $temp_video_dir"
            return
        }
        $video_full_name = Join-Path $output_dir ($temp_video_file.BaseName + ".mp4")
        Write-Host "Downloaded file size: $($temp_video_file.Length) bytes"
        if ($temp_video_file.Length -eq 0) {
            Write-Host "Error: Downloaded file is empty!"
            return
        }

        Write-Host "Translating audio for: $video_link to $temp_audio"
        $translate_result = vot-cli $video_link --output $temp_audio 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Error translating audio: $translate_result"
            return
        }

        $temp_audio_file = (Get-ChildItem -Path $temp_audio -ErrorAction SilentlyContinue)[0].FullName
        if (-not $temp_video_file -or -not $temp_audio_file) {
            Write-Host "Error: Temp video ($temp_video_file) or audio ($temp_audio_file) file missing"
            return
        }

        Write-Host "Merging video and audio into: $video_full_name"
        if ($keep_original) {
            $ffmpeg_result = ffmpeg `
                -i $temp_video_file.FullName -i $temp_audio_file `
                -c:v copy `
                -c:a aac -b:a 128k `
                -map 0:v -map 0:a -map 1:a `
                -metadata:s:a:0 language=orig -metadata:s:a:0 title='Original Audio' `
                -metadata:s:a:1 language=tran -metadata:s:a:1 title='Translated Audio' `
                -y $video_full_name 2>&1
        } else {
            $ffmpeg_result = ffmpeg `
                -i $temp_video_file.FullName -i $temp_audio_file `
                -c:v copy `
                -c:a aac -b:a 128k `
                -map 0:v -map 1:a `
                -y $video_full_name 2>&1
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Error merging with ffmpeg: $ffmpeg_result"
        } else {
            Write-Host "Successfully saved: $video_full_name"
            Write-Host "Final file size: $((Get-Item $video_full_name).Length) bytes"
        }
    } catch {
        Write-Host "Unexpected error in ProcessVideo: $_"
    } finally {
        Write-Host "Cleaning up: $temp_dir"
        Remove-Item -Recurse -Force $temp_dir -ErrorAction SilentlyContinue
    }
}

# Настройки
$original_sound_ratio = 0.1
$quality = "best"

# Обработка аргументов
$video_link = $null
$volume_ratio_arg = $original_sound_ratio
$keep_original = $true
$output_dir = $PWD

if ($args.Length -eq 0) {
    Write-Host "Error: No video link provided."
    exit 1
}

for ($i = 0; $i -lt $args.Length; $i++) {
    if ($args[$i] -match "^https?://") {
        $video_link = $args[$i]
    } elseif ($args[$i] -eq "--keep-original") {
        $keep_original = $true
    } elseif ($args[$i] -eq "--replace-audio") {
        $keep_original = $false
    } elseif ($args[$i] -eq "--output-dir" -and $i + 1 -lt $args.Length) {
        $output_dir = $args[$i + 1]
        $i++
    } elseif ($args[$i] -eq "--quality" -and $i + 1 -lt $args.Length) {
        $quality = $args[$i + 1]
        $i++
    } elseif ($args[$i] -as [double]) {
        $volume_ratio_arg = $args[$i]
    }
}

if (-not $video_link) {
    Write-Host "Error: No valid video link provided."
    exit 1
}

if ($volume_ratio_arg -as [double]) {
    $original_sound_ratio = $volume_ratio_arg
    Write-Host "Original volume is set to $original_sound_ratio (used only for metadata purposes)"
}

Write-Host "Selected video quality: $quality"

# Обработка одного видео
ProcessVideo $video_link $original_sound_ratio $keep_original $output_dir $quality