import { afterEach, describe, expect, it, mock } from "bun:test";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "../../App";
import { checkoutApi, type PixChargeResponse } from "../../lib/api";

const originalFetch = globalThis.fetch;
const originalWebSocket = globalThis.WebSocket;
const originalCreatePixCharge = checkoutApi.createPixCharge;

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];

  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;
  readyState = 0;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  close() {
    this.readyState = 3;
    this.onclose?.({} as CloseEvent);
  }

  open() {
    this.readyState = 1;
    this.onopen?.({} as Event);
  }

  message(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

function installFetchMock(
  implementation: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
) {
  const fetchMock = mock(implementation);
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: fetchMock,
    writable: true,
  });

  return fetchMock;
}

function checkoutResponse() {
  return new Response(
    JSON.stringify({
      agreement: {
        id: "agr_7Nf93",
        status: "AWAITING_PAYMENT",
        customer: {
          name: "Marina Silva",
          email_masked: "ma••••@exemplo.com",
          document_masked: "***.456.789-**",
        },
        amount: "50000.00",
        currency: "BRL",
        delivery_window_days: 7,
        delivery_due_at: null,
        fee_bps: 250,
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

afterEach(() => {
  cleanup();
  window.history.pushState({}, "", "/");
  window.sessionStorage.clear();
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: originalFetch,
    writable: true,
  });
  Object.defineProperty(globalThis, "WebSocket", {
    configurable: true,
    value: originalWebSocket,
    writable: true,
  });
  checkoutApi.createPixCharge = originalCreatePixCharge;
  FakeWebSocket.instances = [];
  mock.restore();
});

describe("checkout público", () => {
  it("abre um acordo sem sessão e apresenta somente os dados do checkout", async () => {
    const fetchMock = installFetchMock(() => Promise.resolve(checkoutResponse()));
    window.history.pushState({}, "", "/checkout/checkout-token-publico");

    render(<App />);

    expect(screen.getByRole("status")).toHaveTextContent("Carregando checkout seguro");

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Revise seu pagamento" })).toBeInTheDocument();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/checkout/checkout-token-publico/",
      expect.objectContaining({ cache: "no-store", credentials: "omit", method: "GET" }),
    );
    expect(screen.getByText(/R\$\s*50\.000,00/)).toBeInTheDocument();
    expect(screen.getByText("Marina Silva")).toBeInTheDocument();
    expect(screen.getByText("ma••••@exemplo.com")).toBeInTheDocument();
    expect(screen.getByText("***.456.789-**")).toBeInTheDocument();
    expect(screen.getByText("Aguardando pagamento PIX")).toBeInTheDocument();
    expect(screen.getByText("7 dias após a confirmação do pagamento")).toBeInTheDocument();
  });

  it("explica quando o link público não existe ou expirou", async () => {
    installFetchMock(() =>
      Promise.resolve(
        new Response(JSON.stringify({ code: "checkout_not_found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    window.history.pushState({}, "", "/checkout/link-inexistente");

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Este checkout não está disponível." }),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/solicite um novo link/i)).toBeInTheDocument();
  });

  it("não expõe detalhes internos quando o checkout falha", async () => {
    installFetchMock(() => Promise.resolve(new Response("falha interna", { status: 500 })));
    window.history.pushState({}, "", "/checkout/checkout-com-erro");

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Não foi possível abrir este checkout." }),
      ).toBeInTheDocument();
    });
    expect(screen.getByText(/tente novamente em alguns instantes/i)).toBeInTheDocument();
    expect(screen.queryByText("falha interna")).not.toBeInTheDocument();
  });

  it("trata HTML de proxy mal configurado como falha segura", async () => {
    installFetchMock(() => Promise.resolve(new Response("<html>checkout</html>", { status: 200 })));
    window.history.pushState({}, "", "/checkout/resposta-invalida");

    render(<App />);

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Não foi possível abrir este checkout." }),
      ).toBeInTheDocument();
    });
    expect(screen.queryByText("<html>checkout</html>")).not.toBeInTheDocument();
  });

  it("mapeia estados canônicos de processamento e custódia", async () => {
    installFetchMock(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            agreement: {
              id: "agr-held",
              status: "HELD",
              customer: {
                name: "Marina Silva",
                email_masked: "ma••••@exemplo.com",
                document_masked: "***.456.789-**",
              },
              amount: "50000.00",
              currency: "BRL",
              delivery_window_days: 7,
              delivery_due_at: null,
              fee_bps: 200,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    window.history.pushState({}, "", "/checkout/acordo-held");

    render(<App />);

    await waitFor(() => {
      expect(screen.getByText("Valor protegido em custódia")).toBeInTheDocument();
    });
    expect(screen.getByText("3. Custódia")).toHaveClass("is-current");
  });

  it("gera uma cobrança PIX e mostra o código copia e cola sem exibir o token", async () => {
    const createCalls: Array<{ idempotencyKey: string; token: string }> = [];
    installFetchMock(() => Promise.resolve(checkoutResponse()));
    checkoutApi.createPixCharge = async (
      checkoutToken,
      idempotencyKey,
    ): Promise<PixChargeResponse> => {
      createCalls.push({ idempotencyKey, token: checkoutToken });
      return {
        status: "PROCESSING",
        pix: {
          id: "pix_123",
          copy_paste: "000201010212BR.GOV.BCB.PIX",
          status: "PENDING",
        },
      };
    };
    window.history.pushState({}, "", "/checkout/checkout-token-publico");

    render(<App />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Gerar código PIX" })).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Gerar código PIX" }));

    await waitFor(() => {
      expect(screen.getByDisplayValue("000201010212BR.GOV.BCB.PIX")).toBeInTheDocument();
    });

    expect(screen.getByRole("button", { name: "Copiar código PIX" })).toBeInTheDocument();
    expect(createCalls).toHaveLength(1);
    expect(createCalls[0]?.token).toBe("checkout-token-publico");
    expect(createCalls[0]?.idempotencyKey).toMatch(/^pix:/);
    expect(window.sessionStorage.getItem("escrow.pix.idempotency.checkout-token-publico")).toBe(
      createCalls[0]?.idempotencyKey,
    );
    expect(document.body.textContent).not.toContain("checkout-token-publico");
  });

  it("reconcilia atualizações de status pelo WebSocket e busca um snapshot ao detectar lacuna", async () => {
    let snapshotRequests = 0;
    installFetchMock((_input, init) => {
      if (init?.method === "GET") {
        snapshotRequests += 1;
      }
      return Promise.resolve(checkoutResponse());
    });
    Object.defineProperty(globalThis, "WebSocket", {
      configurable: true,
      value: FakeWebSocket,
      writable: true,
    });
    window.history.pushState({}, "", "/checkout/checkout-token-realtime");

    render(<App />);

    await waitFor(() => {
      expect(FakeWebSocket.instances).toHaveLength(1);
    });
    expect(FakeWebSocket.instances[0]?.url).toBe(
      "ws://localhost:5173/ws/checkout/checkout-token-realtime/",
    );

    await act(async () => {
      FakeWebSocket.instances[0]?.open();
    });
    await waitFor(() => {
      expect(snapshotRequests).toBeGreaterThanOrEqual(2);
    });

    await act(async () => {
      FakeWebSocket.instances[0]?.message({
        agreement_id: "agr_7Nf93",
        sequence: 1,
        status: "FUNDING_PROCESSING",
      });
    });
    await waitFor(() => {
      expect(screen.getByText("Pagamento em análise")).toBeInTheDocument();
    });

    const requestsBeforeGap = snapshotRequests;
    await act(async () => {
      FakeWebSocket.instances[0]?.message({
        agreement_id: "agr_7Nf93",
        sequence: 3,
        status: "HELD",
      });
    });
    await waitFor(() => {
      expect(snapshotRequests).toBeGreaterThan(requestsBeforeGap);
    });
  });
});
