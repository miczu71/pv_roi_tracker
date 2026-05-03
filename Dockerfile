ARG BUILD_FROM
FROM $BUILD_FROM

ENV LANG=C.UTF-8

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY pv_roi_tracker/ ./pv_roi_tracker/
COPY run.sh /
RUN chmod +x /run.sh

CMD ["/run.sh"]
