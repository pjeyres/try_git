FROM node:22-alpine
WORKDIR /app
COPY package.json server.js store.js ./
COPY public ./public
ENV PORT=3000 DATA_FILE=/data/db.json
VOLUME /data
EXPOSE 3000
USER node
CMD ["node", "server.js"]
