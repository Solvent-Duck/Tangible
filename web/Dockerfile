# Web app image. Build from the repo root:
#   docker build -f web/Dockerfile --build-arg TANGIBLE_API_URL=https://api.example.test \
#     --build-arg NEXT_PUBLIC_AUTH_MODE=clerk --build-arg NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=pk_... -t tangible-web web
# NEXT_PUBLIC_* values and the /api rewrite target are baked in at build time; CLERK_SECRET_KEY is
# a runtime variable.
FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG TANGIBLE_API_URL=http://127.0.0.1:8000
ARG NEXT_PUBLIC_AUTH_MODE=dev
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=
ARG NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in
ARG NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up
ARG NEXT_PUBLIC_DEMO_LOGIN=
ENV TANGIBLE_API_URL=$TANGIBLE_API_URL \
    NEXT_PUBLIC_AUTH_MODE=$NEXT_PUBLIC_AUTH_MODE \
    NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY \
    NEXT_PUBLIC_CLERK_SIGN_IN_URL=$NEXT_PUBLIC_CLERK_SIGN_IN_URL \
    NEXT_PUBLIC_CLERK_SIGN_UP_URL=$NEXT_PUBLIC_CLERK_SIGN_UP_URL \
    NEXT_PUBLIC_DEMO_LOGIN=$NEXT_PUBLIC_DEMO_LOGIN \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 HOSTNAME=0.0.0.0 PORT=3000
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
COPY --from=build /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
