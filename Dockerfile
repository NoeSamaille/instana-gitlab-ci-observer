FROM registry.access.redhat.com/ubi8/python-39:1

USER 1001

WORKDIR /opt/app-root/src

COPY --chown=1001 . .

RUN pip3 install --upgrade pip && \
    pip3 install -r requirements.txt

ENV PORT 8088

EXPOSE 8088

CMD ["python3", "main.py"]
