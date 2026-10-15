// Service worker mínimo — solo cumple el requisito de instalación.
// No cachea nada: cada request va siempre a la red.
self.addEventListener("fetch", () => {});

