# Demo UI image for 'azd up' (standard library only, no packages to install)
FROM mcr.microsoft.com/azurelinux/base/python:3.12
WORKDIR /app
COPY app.py ./
COPY static ./static
ENV HOST=0.0.0.0 PORT=80 PYTHONUNBUFFERED=1
EXPOSE 80
CMD ["python3", "app.py"]
