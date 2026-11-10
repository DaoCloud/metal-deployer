# DHCP server for the LOM network in the S3/S4 end-to-end test.
FROM docker:cli
RUN apk add --no-cache dnsmasq
ENTRYPOINT ["dnsmasq", "-d", "--port=0", "--log-dhcp"]
