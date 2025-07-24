from flask import Flask, request, render_template
import os
from werkzeug.utils import secure_filename
from process import process_video  # Imports your existing logic

UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'mp3'}

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        link = request.form.get('link')
        file = request.files.get('file')

        try:
            if link:
                process_video(link=link)
                return render_template('index.html', success="Processed YouTube/Twitch link.")

            elif file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                process_video(file_path=filepath)
                return render_template('index.html', success="Processed uploaded file.")

            else:
                return render_template('index.html', error="No valid input provided.")
        except Exception as e:
            return render_template('index.html', error=str(e))

    return render_template('index.html')

if __name__ == "__main__":
    app.run(debug=True)
