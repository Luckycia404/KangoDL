FROM node:24-slim
ENV NODE_ENV=production POT_HOST=0.0.0.0 POT_PORT=8878
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY server.js ./
USER node
EXPOSE 8878
CMD ["node", "server.js"]
