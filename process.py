import yt_dlp
from pytube import YouTube
from moviepy.editor import VideoFileClip
import whisper
from textblob import TextBlob
import pandas as pd
import os
from openpyxl import load_workbook

# def download_youtube_audio(url):
#     yt = YouTube(url)
#     stream = yt.streams.filter(only_audio=True).first()
#     filepath = "yt_audio.mp4"
#     stream.download(filename=filepath)
#     return filepath
def download_youtube_audio(url):
    output_path = "yt_audio.%(ext)s"  # use .%(ext)s for proper extension

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': output_path,
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'quiet': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    return "yt_audio.mp3"




# def extract_audio(video_path):
#     video = VideoFileClip(video_path)
#     audio_path = "audio.wav"
#     video.audio.write_audiofile(audio_path)
#     return audio_path
def extract_audio(video_path):
    # If already .mp3, return as is
    if video_path.endswith(".mp3"):
        return video_path

    video = VideoFileClip(video_path)
    audio_path = "audio.wav"
    video.audio.write_audiofile(audio_path)
    return audio_path


def transcribe_audio(audio_path):
    model = whisper.load_model("base")
    result = model.transcribe(audio_path)
    return result["text"]

def analyze_text(text):
    blob = TextBlob(text)
    sentiment = blob.sentiment.polarity
    keywords = ", ".join(blob.noun_phrases)
    return sentiment, keywords

# def save_to_excel(platform, link, transcript, sentiment, keywords):
#     df = pd.DataFrame([{
#         "Platform": platform,
#         "Link/File": link,
#         "Transcript": transcript,
#         "Sentiment": sentiment,
#         "Keywords": keywords
#     }])
#     df.to_excel("media_report.xlsx", index=False)

def save_to_excel(platform, target, transcript, sentiment, keywords):
    df = pd.DataFrame([{
        "Platform": platform,
        "Target": target,
        "Transcript": transcript,
        "Sentiment": sentiment,
        "Keywords": ", ".join(keywords)
    }])

    file_path = "media_report.xlsx"

    if os.path.exists(file_path):
        # Load the workbook and get the current max row
        book = load_workbook(file_path)
        sheet = book.active
        start_row = sheet.max_row

        with pd.ExcelWriter(file_path, engine='openpyxl', mode='a', if_sheet_exists='overlay') as writer:
            df.to_excel(writer, index=False, header=False, startrow=start_row)
    else:
        # Create a new file with headers
        df.to_excel(file_path, index=False)


def process_video(link=None, file_path=None):
    transcript = ""
    platform = ""
    target = ""

    if link:
        platform = "YouTube"
        target = link
        video_path = download_youtube_audio(link)
    elif file_path and os.path.exists(file_path):
        platform = "Uploaded Video"
        target = file_path
        video_path = file_path
    else:
        raise ValueError("Provide a YouTube link or upload a video file.")

    audio_path = extract_audio(video_path)
    transcript = transcribe_audio(audio_path)
    sentiment, keywords = analyze_text(transcript)
    save_to_excel(platform, target, transcript, sentiment, keywords)

    print("Analysis complete. Saved to media_report.xlsx")

if __name__ == "__main__":
    # process_video(link="https://www.youtube.com/watch?v=2vjPBrBU-TM")  # Sia - Chandelier (example public video)
    process_video(link="https://www.twitch.tv/s0mcs/clip/WonderfulCleverBasenjiDoubleRainbow-I8GL2rAJttQodE41") 