# ----------------------------------------------------
# Dockerfile for Mochiko AI Assistant
# Base Image: Use an official, slim Python 3.11 image
# ----------------------------------------------------
FROM python:3.11-slim

# Set environment variables to prevent caching and ensure logs are output correctly
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONUNBUFFERED=1

# Set the working directory inside the container
WORKDIR /app

# Copy the requirements file first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install -r requirements.txt

# Copy the rest of the application code into the container
COPY . .

# Expose the port the app runs on
EXPOSE 10000

# The command to run the application using a production server
CMD ["gunicorn", "--bind", "0.0.0.0:10000", "app:application"]
