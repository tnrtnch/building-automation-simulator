FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY room_model.py house_model.py simulator.py ./
CMD ["python", "-u", "simulator.py"]