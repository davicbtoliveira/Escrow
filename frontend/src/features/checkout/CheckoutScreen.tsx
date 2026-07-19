import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, checkoutApi, type PixChargeResponse, type PublicCheckout } from "../../lib/api";

type CheckoutScreenProps = {
  token: string;
};

type CheckoutState =
  | { status: "loading" }
  | { status: "ready"; checkout: PublicCheckout }
  | { status: "not-found" }
  | { status: "error" };

type CheckoutStatus = {
  detail: string;
  label: string;
  tone: "held" | "pending" | "rejected" | "released" | "review";
};

type PixCharge = PixChargeResponse["pix"];

type CheckoutPayment = NonNullable<PublicCheckout["payment"]>;

type DeliveryAcceptanceStage =
  | "idle"
  | "sending"
  | "code"
  | "verifying"
  | "authorized"
  | "accepting"
  | "submitted";

type CheckoutStatusEvent = {
  agreementId: string;
  sequence: number;
  status: string;
};

const checkoutStatus: Record<string, CheckoutStatus> = {
  AWAITING_PAYMENT: {
    label: "Aguardando pagamento PIX",
    detail: "Gere o código PIX para iniciar o pagamento protegido.",
    tone: "pending",
  },
  PENDING_FUNDING: {
    label: "Aguardando pagamento PIX",
    detail: "Gere o código PIX para iniciar o pagamento protegido.",
    tone: "pending",
  },
  FUNDING_PROCESSING: {
    label: "Pagamento em análise",
    detail: "O pagamento foi recebido e está passando pela análise de segurança.",
    tone: "review",
  },
  PENDING_RISK_REVIEW: {
    label: "Pagamento em análise",
    detail: "O pagamento foi recebido e está passando pela análise de segurança.",
    tone: "review",
  },
  REVIEW_REQUIRED: {
    label: "Análise de risco necessária",
    detail: "A equipe de risco está revisando este pagamento antes da custódia.",
    tone: "review",
  },
  HELD: {
    label: "Valor protegido em custódia",
    detail: "O pagamento foi aprovado e permanece protegido até a confirmação de entrega.",
    tone: "held",
  },
  HELD_IN_ESCROW: {
    label: "Valor protegido em custódia",
    detail: "O pagamento foi aprovado e permanece protegido até a confirmação de entrega.",
    tone: "held",
  },
  INSPECTION: {
    label: "Entrega informada",
    detail: "Você tem uma janela de inspeção para confirmar a entrega ou abrir uma disputa.",
    tone: "held",
  },
  RELEASE_PENDING: {
    label: "Liberação em processamento",
    detail: "Sua confirmação foi recebida e o valor está sendo liberado para a organização.",
    tone: "review",
  },
  REFUND_PENDING: {
    label: "Reembolso em processamento",
    detail: "A organização não entregou dentro do prazo combinado. O valor está sendo devolvido.",
    tone: "review",
  },
  REFUNDED: {
    label: "Valor reembolsado",
    detail:
      "A organização não entregou dentro do prazo combinado. O valor protegido foi devolvido.",
    tone: "rejected",
  },
  FUNDING_REJECTED: {
    label: "Pagamento não aprovado",
    detail: "O valor não foi colocado em custódia. Solicite uma nova orientação à organização.",
    tone: "rejected",
  },
  RELEASED: {
    label: "Valor liberado",
    detail: "A entrega foi confirmada e o valor foi liberado para a organização.",
    tone: "released",
  },
};

const unknownStatus: CheckoutStatus = {
  label: "Acordo em processamento",
  detail: "Estamos atualizando os dados deste acordo protegido.",
  tone: "pending",
};

function checkoutStep(status: string): number {
  if (
    status === "RELEASED" ||
    status === "RELEASE_PENDING" ||
    status === "REFUNDED" ||
    status === "REFUND_PENDING"
  ) {
    return 3;
  }
  if (status === "HELD_IN_ESCROW" || status === "HELD" || status === "INSPECTION") {
    return 2;
  }
  if (
    status === "PENDING_RISK_REVIEW" ||
    status === "FUNDING_PROCESSING" ||
    status === "REVIEW_REQUIRED"
  ) {
    return 1;
  }
  return 0;
}

function displayMoney(amount: string, currency: PublicCheckout["agreement"]["currency"]): string {
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(amount);

  if (!match) {
    return "—";
  }

  const [, integer, decimal = ""] = match;
  const major = new Intl.NumberFormat("pt-BR", {
    style: "currency",
    currency,
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(BigInt(integer));

  return `${major},${decimal.padEnd(2, "0")}`;
}

function displayFee(feeBps: number): string {
  return new Intl.NumberFormat("pt-BR", {
    style: "percent",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(feeBps / 10_000);
}

function deliveryWindow(copy: PublicCheckout["agreement"]): string {
  if (copy.delivery_due_at) {
    return new Intl.DateTimeFormat("pt-BR", {
      dateStyle: "long",
      timeZone: "UTC",
    }).format(new Date(copy.delivery_due_at));
  }

  return `${copy.delivery_window_days} dias após a confirmação do pagamento`;
}

function inspectionWindow(copy: PublicCheckout["agreement"]): string | undefined {
  if (!copy.inspection_deadline_at) {
    return undefined;
  }
  return new Intl.DateTimeFormat("pt-BR", {
    dateStyle: "long",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(new Date(copy.inspection_deadline_at));
}

function paymentFor(checkout: PublicCheckout): CheckoutPayment | undefined {
  return checkout.payment;
}

function sequenceFor(checkout: PublicCheckout): number | undefined {
  const sequence = checkout.agreement.realtime_sequence;
  return typeof sequence === "number" && Number.isSafeInteger(sequence) && sequence >= 0
    ? sequence
    : undefined;
}

function parseStatusEvent(value: unknown): CheckoutStatusEvent | undefined {
  if (typeof value !== "string") {
    return undefined;
  }

  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || typeof parsed !== "object") {
      return undefined;
    }
    const event = parsed as Record<string, unknown>;
    if (
      typeof event.agreement_id !== "string" ||
      typeof event.status !== "string" ||
      typeof event.sequence !== "number" ||
      !Number.isSafeInteger(event.sequence) ||
      event.sequence < 1
    ) {
      return undefined;
    }
    return {
      agreementId: event.agreement_id,
      sequence: event.sequence,
      status: event.status,
    };
  } catch {
    return undefined;
  }
}

function checkoutSocketUrl(token: string): string {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  return `${protocol}://${window.location.host}/ws/checkout/${encodeURIComponent(token)}/`;
}

function isTerminalStatus(status: string): boolean {
  return ["FUNDING_REJECTED", "CANCELLED", "REFUNDED", "RELEASED"].includes(status);
}

function isProcessingStatus(status: string): boolean {
  return [
    "FUNDING_PROCESSING",
    "PENDING_RISK_REVIEW",
    "REVIEW_REQUIRED",
    "RELEASE_PENDING",
    "REFUND_PENDING",
  ].includes(status);
}

function idempotencyStorageKey(token: string): string {
  return `escrow.pix.idempotency.${token}`;
}

function newIdempotencyKey(): string {
  const secureCrypto = globalThis.crypto;
  if (typeof secureCrypto?.randomUUID !== "function") {
    throw new Error("Secure random values are unavailable.");
  }
  return `pix:${secureCrypto.randomUUID()}`;
}

function pixIdempotencyKey(token: string): string {
  const storageKey = idempotencyStorageKey(token);
  try {
    const existing = window.sessionStorage.getItem(storageKey);
    if (existing) {
      return existing;
    }

    const key = newIdempotencyKey();
    window.sessionStorage.setItem(storageKey, key);
    return key;
  } catch {
    return newIdempotencyKey();
  }
}

async function createPixCharge(token: string, idempotencyKey: string): Promise<PixChargeResponse> {
  return checkoutApi.createPixCharge(token, idempotencyKey);
}

export function CheckoutScreen({ token }: CheckoutScreenProps) {
  const [state, setState] = useState<CheckoutState>({ status: "loading" });
  const [creatingPix, setCreatingPix] = useState(false);
  const [pix, setPix] = useState<PixCharge | undefined>();
  const [pixError, setPixError] = useState<string | undefined>();
  const [copyFeedback, setCopyFeedback] = useState<string | undefined>();
  const [deliveryAcceptanceStage, setDeliveryAcceptanceStage] =
    useState<DeliveryAcceptanceStage>("idle");
  const [deliveryChallengeId, setDeliveryChallengeId] = useState<string | undefined>();
  const [deliveryAcceptanceToken, setDeliveryAcceptanceToken] = useState<string | undefined>();
  const [deliveryOtpCode, setDeliveryOtpCode] = useState("");
  const [deliveryAcceptanceError, setDeliveryAcceptanceError] = useState<string | undefined>();
  const [realtimeConnected, setRealtimeConnected] = useState(false);
  const lastSequence = useRef<number | undefined>(undefined);

  const loadCheckout = useCallback(
    async (mode: "initial" | "refresh" = "initial") => {
      if (mode === "initial") {
        setState({ status: "loading" });
      }

      try {
        const checkout = await checkoutApi.get(token);
        const sequence = sequenceFor(checkout);
        if (sequence !== undefined) {
          lastSequence.current = sequence;
        }
        setState({ status: "ready", checkout });
      } catch (error) {
        if (mode === "refresh") {
          return;
        }
        if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
          setState({ status: "not-found" });
          return;
        }

        setState({ status: "error" });
      }
    },
    [token],
  );

  useEffect(() => {
    lastSequence.current = undefined;
    setPix(undefined);
    setPixError(undefined);
    setCopyFeedback(undefined);
    setDeliveryAcceptanceStage("idle");
    setDeliveryChallengeId(undefined);
    setDeliveryAcceptanceToken(undefined);
    setDeliveryOtpCode("");
    setDeliveryAcceptanceError(undefined);
    void loadCheckout();
  }, [loadCheckout]);

  const agreement = state.status === "ready" ? state.checkout.agreement : undefined;
  const agreementId = agreement?.id;
  const shouldSubscribe = Boolean(agreement && !isTerminalStatus(agreement.status));
  const shouldPoll = Boolean(
    agreement && isProcessingStatus(agreement.status) && !realtimeConnected,
  );

  useEffect(() => {
    if (!shouldSubscribe || !agreementId || typeof WebSocket === "undefined") {
      setRealtimeConnected(false);
      return;
    }

    let disposed = false;
    let retryAttempt = 0;
    let reconnectTimer: number | undefined;
    let socket: WebSocket | undefined;

    const scheduleReconnect = () => {
      if (disposed) {
        return;
      }
      setRealtimeConnected(false);
      const delay = Math.min(1_000 * 2 ** retryAttempt, 15_000);
      retryAttempt += 1;
      reconnectTimer = window.setTimeout(connect, delay);
    };

    const connect = () => {
      if (disposed) {
        return;
      }
      let nextSocket: WebSocket;
      try {
        nextSocket = new WebSocket(checkoutSocketUrl(token));
      } catch {
        scheduleReconnect();
        return;
      }
      socket = nextSocket;

      nextSocket.onopen = () => {
        if (disposed) {
          return;
        }
        retryAttempt = 0;
        setRealtimeConnected(true);
        void loadCheckout("refresh");
      };

      nextSocket.onmessage = (message) => {
        const event = parseStatusEvent(message.data);
        if (!event || event.agreementId !== agreementId) {
          return;
        }
        const previousSequence = lastSequence.current;
        if (previousSequence !== undefined && event.sequence <= previousSequence) {
          return;
        }
        lastSequence.current = event.sequence;
        if (previousSequence !== undefined && event.sequence > previousSequence + 1) {
          void loadCheckout("refresh");
          return;
        }
        setState((current) => {
          if (current.status !== "ready" || current.checkout.agreement.id !== event.agreementId) {
            return current;
          }
          return {
            status: "ready",
            checkout: {
              ...current.checkout,
              agreement: { ...current.checkout.agreement, status: event.status },
            },
          };
        });
      };

      nextSocket.onerror = () => nextSocket.close();
      nextSocket.onclose = () => {
        if (socket === nextSocket) {
          scheduleReconnect();
        }
      };
    };

    connect();

    return () => {
      disposed = true;
      if (reconnectTimer !== undefined) {
        window.clearTimeout(reconnectTimer);
      }
      socket?.close();
    };
  }, [agreementId, loadCheckout, shouldSubscribe, token]);

  useEffect(() => {
    if (!shouldPoll) {
      return;
    }
    const timer = window.setInterval(() => {
      void loadCheckout("refresh");
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [loadCheckout, shouldPoll]);

  const handleCreatePix = useCallback(async () => {
    setCreatingPix(true);
    setPixError(undefined);
    try {
      const response = await createPixCharge(token, pixIdempotencyKey(token));
      setPix(response.pix);
      void loadCheckout("refresh");
    } catch {
      setPixError("Não foi possível gerar o código PIX. Tente novamente.");
    } finally {
      setCreatingPix(false);
    }
  }, [loadCheckout, token]);

  const handleCopyPix = useCallback(async (copyPaste: string) => {
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard API unavailable.");
      }
      await navigator.clipboard.writeText(copyPaste);
      setCopyFeedback("Código PIX copiado.");
    } catch {
      setCopyFeedback("Selecione e copie o código PIX manualmente.");
    }
  }, []);

  const handleRequestDeliveryOtp = useCallback(async () => {
    setDeliveryAcceptanceStage("sending");
    setDeliveryAcceptanceError(undefined);
    try {
      const challenge = await checkoutApi.requestDeliveryAcceptanceOtp(token);
      setDeliveryChallengeId(challenge.challenge_id);
      setDeliveryAcceptanceStage("code");
    } catch {
      setDeliveryAcceptanceStage("idle");
      setDeliveryAcceptanceError("Não foi possível enviar o código agora. Tente novamente.");
    }
  }, [token]);

  const handleVerifyDeliveryOtp = useCallback(async () => {
    if (!deliveryChallengeId) {
      return;
    }
    setDeliveryAcceptanceStage("verifying");
    setDeliveryAcceptanceError(undefined);
    try {
      const authorization = await checkoutApi.verifyDeliveryAcceptanceOtp(
        token,
        deliveryChallengeId,
        deliveryOtpCode,
      );
      setDeliveryAcceptanceToken(authorization.acceptance_token);
      setDeliveryAcceptanceStage("authorized");
    } catch {
      setDeliveryAcceptanceStage("code");
      setDeliveryAcceptanceError(
        "Código inválido ou expirado. Solicite outro código se necessário.",
      );
    }
  }, [deliveryChallengeId, deliveryOtpCode, token]);

  const handleAcceptDelivery = useCallback(async () => {
    if (!deliveryChallengeId || !deliveryAcceptanceToken) {
      return;
    }
    setDeliveryAcceptanceStage("accepting");
    setDeliveryAcceptanceError(undefined);
    try {
      await checkoutApi.acceptReportedDelivery(token, deliveryChallengeId, deliveryAcceptanceToken);
      setDeliveryAcceptanceStage("submitted");
      void loadCheckout("refresh");
    } catch {
      setDeliveryAcceptanceStage("authorized");
      setDeliveryAcceptanceError("Não foi possível iniciar a liberação agora. Tente novamente.");
    }
  }, [deliveryAcceptanceToken, deliveryChallengeId, loadCheckout, token]);

  if (state.status === "loading") {
    return <CheckoutLoading />;
  }

  if (state.status === "not-found") {
    return <CheckoutUnavailable kind="not-found" onRetry={loadCheckout} />;
  }

  if (state.status === "error") {
    return <CheckoutUnavailable kind="error" onRetry={loadCheckout} />;
  }

  const { checkout } = state;
  const { agreement: readyAgreement } = checkout;
  const status = checkoutStatus[readyAgreement.status] ?? unknownStatus;
  const currentStep = checkoutStep(readyAgreement.status);
  const payment = paymentFor(checkout);
  const copyPaste = payment?.pix_copy_paste ?? pix?.copy_paste;
  const pixStatus = payment?.status ?? pix?.status;
  const canCreatePix = readyAgreement.status === "AWAITING_PAYMENT" && !copyPaste && !payment;

  return (
    <main className="checkout-shell">
      <header className="checkout-topbar">
        <a href="/" aria-label="Escrow — início">
          <span className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">
              <i />
              <i />
              <i />
            </span>
            <span>ESCROW</span>
          </span>
        </a>
        <p>CHECKOUT PROTEGIDO · PIX</p>
      </header>

      <section className="checkout-layout" aria-labelledby="checkout-title">
        <div className="checkout-introduction">
          <p className="eyebrow">PAGAMENTO COM CUSTÓDIA</p>
          <h1 id="checkout-title">Revise seu pagamento</h1>
          <p className="checkout-lede">
            Seu pagamento fica protegido em custódia. A organização só recebe o valor depois da
            confirmação da entrega.
          </p>

          <ol className="checkout-steps" aria-label="Etapas do pagamento protegido">
            <li className={currentStep >= 0 ? "is-current" : undefined}>1. Pagamento PIX</li>
            <li className={currentStep >= 1 ? "is-current" : undefined}>2. Análise</li>
            <li className={currentStep >= 2 ? "is-current" : undefined}>3. Custódia</li>
            <li className={currentStep >= 3 ? "is-current" : undefined}>4. Liberação</li>
          </ol>
        </div>

        <section className="checkout-card" aria-label="Resumo do acordo de custódia">
          <div className="checkout-amount">
            <span>VALOR DO ACORDO</span>
            <output>{displayMoney(readyAgreement.amount, readyAgreement.currency)}</output>
          </div>

          <div className={`checkout-status checkout-status-${status.tone}`} role="status">
            <span className="checkout-status-mark" aria-hidden="true" />
            <div>
              <strong>{status.label}</strong>
              <p>{status.detail}</p>
            </div>
          </div>

          {canCreatePix || copyPaste || pixError ? (
            <PixPaymentPanel
              copyFeedback={copyFeedback}
              copyPaste={copyPaste}
              creating={creatingPix}
              error={pixError}
              onCopy={handleCopyPix}
              onCreate={handleCreatePix}
              pixStatus={pixStatus}
              showCreate={canCreatePix}
            />
          ) : null}

          {readyAgreement.status === "INSPECTION" ? (
            <DeliveryAcceptancePanel
              code={deliveryOtpCode}
              error={deliveryAcceptanceError}
              onAccept={() => void handleAcceptDelivery()}
              onCodeChange={setDeliveryOtpCode}
              onRequestOtp={() => void handleRequestDeliveryOtp()}
              onVerifyOtp={() => void handleVerifyDeliveryOtp()}
              stage={deliveryAcceptanceStage}
            />
          ) : null}

          <dl className="checkout-details">
            <div>
              <dt>Pagador</dt>
              <dd>{readyAgreement.customer.name}</dd>
            </div>
            <div>
              <dt>E-mail</dt>
              <dd>{readyAgreement.customer.email_masked}</dd>
            </div>
            <div>
              <dt>Documento</dt>
              <dd>{readyAgreement.customer.document_masked}</dd>
            </div>
            <div>
              <dt>Prazo para entrega</dt>
              <dd>{deliveryWindow(readyAgreement)}</dd>
            </div>
            {inspectionWindow(readyAgreement) ? (
              <div>
                <dt>Inspeção até</dt>
                <dd>{inspectionWindow(readyAgreement)}</dd>
              </div>
            ) : null}
            <div>
              <dt>Taxa de custódia</dt>
              <dd>{displayFee(readyAgreement.fee_bps)}</dd>
            </div>
            <div>
              <dt>Referência</dt>
              <dd>{readyAgreement.id}</dd>
            </div>
          </dl>

          <p className="checkout-disclosure">
            O código PIX aparece apenas nesta tela. Não compartilhe este link com terceiros.
          </p>
        </section>
      </section>
    </main>
  );
}

function DeliveryAcceptancePanel({
  code,
  error,
  onAccept,
  onCodeChange,
  onRequestOtp,
  onVerifyOtp,
  stage,
}: {
  code: string;
  error: string | undefined;
  onAccept: () => void;
  onCodeChange: (value: string) => void;
  onRequestOtp: () => void;
  onVerifyOtp: () => void;
  stage: DeliveryAcceptanceStage;
}) {
  const isRequesting = stage === "sending";
  const isVerifying = stage === "verifying";
  const isAccepting = stage === "accepting";
  const isCodeStage = stage === "code" || isVerifying;

  return (
    <section className="delivery-acceptance" aria-labelledby="delivery-acceptance-title">
      <p className="checkout-pix-kicker">CONFIRMAÇÃO DA ENTREGA</p>
      <h2 id="delivery-acceptance-title">Recebeu o pedido?</h2>
      <p>Para proteger seu pagamento, confirme a entrega com o código enviado para o seu e-mail.</p>

      {stage === "idle" || isRequesting ? (
        <button
          className="primary-action checkout-pix-action"
          disabled={isRequesting}
          type="button"
          onClick={onRequestOtp}
        >
          {isRequesting ? "Enviando código…" : "Receber código por e-mail"}
        </button>
      ) : null}

      {isCodeStage ? (
        <div className="delivery-acceptance-code">
          <label htmlFor="delivery-acceptance-code">Código de confirmação</label>
          <input
            autoComplete="one-time-code"
            id="delivery-acceptance-code"
            inputMode="numeric"
            maxLength={6}
            onChange={(event) => onCodeChange(event.target.value.replace(/\D/g, ""))}
            value={code}
          />
          <button
            className="secondary-action checkout-pix-copy-action"
            disabled={isVerifying || code.length !== 6}
            type="button"
            onClick={onVerifyOtp}
          >
            {isVerifying ? "Validando código…" : "Validar código"}
          </button>
        </div>
      ) : null}

      {stage === "authorized" || isAccepting ? (
        <button
          className="primary-action checkout-pix-action"
          disabled={isAccepting}
          type="button"
          onClick={onAccept}
        >
          {isAccepting ? "Iniciando liberação…" : "Confirmar entrega"}
        </button>
      ) : null}

      {stage === "submitted" ? (
        <p className="checkout-pix-feedback" role="status">
          Confirmação recebida. A liberação está sendo processada.
        </p>
      ) : null}
      {error ? (
        <p className="checkout-pix-feedback checkout-pix-feedback-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}

function PixPaymentPanel({
  copyFeedback,
  copyPaste,
  creating,
  error,
  onCopy,
  onCreate,
  pixStatus,
  showCreate,
}: {
  copyFeedback: string | undefined;
  copyPaste: string | undefined;
  creating: boolean;
  error: string | undefined;
  onCopy: (copyPaste: string) => Promise<void>;
  onCreate: () => Promise<void>;
  pixStatus: string | undefined;
  showCreate: boolean;
}) {
  return (
    <section className="checkout-pix" aria-labelledby="pix-title" aria-busy={creating}>
      <div className="checkout-pix-heading">
        <div>
          <p className="checkout-pix-kicker">PAGAMENTO PIX</p>
          <h2 id="pix-title">{copyPaste ? "Pague com PIX" : "Gere seu código PIX"}</h2>
        </div>
        {pixStatus ? <span className="checkout-pix-state">{pixStateCopy(pixStatus)}</span> : null}
      </div>

      {showCreate ? (
        <>
          <p className="checkout-pix-copy">
            Gere uma cobrança única. O valor só seguirá para custódia após a confirmação e a análise
            de segurança.
          </p>
          <button
            className="primary-action checkout-pix-action"
            disabled={creating}
            type="button"
            onClick={() => void onCreate()}
          >
            {creating ? "Gerando código PIX…" : "Gerar código PIX"}
          </button>
        </>
      ) : null}

      {copyPaste ? (
        <div className="checkout-pix-code">
          <label htmlFor="pix-copy-paste">Código PIX copia e cola</label>
          <textarea
            readOnly
            aria-label="Código PIX copia e cola"
            id="pix-copy-paste"
            spellCheck={false}
            value={copyPaste}
          />
          <button
            className="secondary-action checkout-pix-copy-action"
            type="button"
            onClick={() => void onCopy(copyPaste)}
          >
            Copiar código PIX
          </button>
        </div>
      ) : null}

      {error ? (
        <p className="checkout-pix-feedback checkout-pix-feedback-error" role="alert">
          {error}
        </p>
      ) : null}
      {copyFeedback ? (
        <p className="checkout-pix-feedback" role="status">
          {copyFeedback}
        </p>
      ) : null}
    </section>
  );
}

function pixStateCopy(status: string): string {
  if (status === "PENDING") {
    return "AGUARDANDO PAGAMENTO";
  }
  if (status === "CONFIRMED") {
    return "PAGAMENTO CONFIRMADO";
  }
  if (status === "REJECTED") {
    return "PAGAMENTO RECUSADO";
  }
  return "EM PROCESSAMENTO";
}

function CheckoutLoading() {
  return (
    <main className="checkout-shell checkout-center" role="status" aria-live="polite">
      <p className="eyebrow">CHECKOUT PROTEGIDO</p>
      <h1>Carregando checkout seguro</h1>
      <p>Confirmando os detalhes do acordo de custódia.</p>
    </main>
  );
}

function CheckoutUnavailable({
  kind,
  onRetry,
}: {
  kind: "error" | "not-found";
  onRetry: () => Promise<void>;
}) {
  const notFound = kind === "not-found";

  return (
    <main className="checkout-shell checkout-center" role="alert" aria-live="assertive">
      <p className="eyebrow">CHECKOUT PROTEGIDO</p>
      <h1>
        {notFound ? "Este checkout não está disponível." : "Não foi possível abrir este checkout."}
      </h1>
      <p>
        {notFound
          ? "O link pode ter expirado ou já ter sido utilizado. Solicite um novo link à organização."
          : "Tente novamente em alguns instantes. Seus dados e seu pagamento continuam protegidos."}
      </p>
      {!notFound ? (
        <button
          className="primary-action checkout-retry"
          type="button"
          onClick={() => void onRetry()}
        >
          Tentar novamente
        </button>
      ) : null}
    </main>
  );
}
