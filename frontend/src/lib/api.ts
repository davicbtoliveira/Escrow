import type { components } from "./generated/openapi";

export type ScheduledRelease = {
  id: string;
  gross_minor: number;
  fee_minor: number;
  net_minor: number;
  currency: "BRL";
  release_at: string;
};

export type CurrentOrganization = {
  organization: {
    name: string;
    document_masked: string | null;
  };
  membership: {
    role: "OWNER" | "FINANCE" | "SUPPORT" | "VIEWER";
  };
  balances: {
    held_brl_minor: number;
    available_brl_minor: number;
  };
  upcoming_releases: ScheduledRelease[];
};

export const apiKeyScopes = [
  "agreements:write",
  "agreements:read",
  "payments:write",
  "payments:read",
  "webhooks:manage",
] as const;

export type ApiKeyScope = (typeof apiKeyScopes)[number];

export type OrganizationApiKey = {
  id: string;
  name: string;
  prefix: string;
  scopes: ApiKeyScope[];
  expires_at: string | null;
  last_used_at: string | null;
  last_used_ip: string | null;
  status: "ACTIVE" | "EXPIRED" | "REVOKED";
};

export type ApiKeyListResponse = {
  api_keys: OrganizationApiKey[];
};

export type ApiKeySecretResponse = {
  api_key: OrganizationApiKey;
  previous_api_key?: OrganizationApiKey;
  secret: string;
};

export type CreateApiKeyInput = {
  name: string;
  scopes: ApiKeyScope[];
  expires_at?: string;
};

export type RegisterInput = {
  organization_name: string;
  email: string;
  password: string;
  password_confirmation: string;
};

export type PasswordRecoveryConfirmationInput = {
  uid: string;
  token: string;
  password: string;
  password_confirmation: string;
};

/** Generated from the backend OpenAPI contract via `bun run api:generate`. */
export type PublicCheckout = components["schemas"]["PublicCheckoutResponse"];

export type PixChargeResponse = {
  status: "PROCESSING";
  pix: {
    id: string;
    copy_paste: string;
    status: "PENDING" | "CONFIRMED" | "REJECTED";
  };
};

export type CustomerOtpChallenge = {
  challenge_id: string;
  expires_at: string;
};

export type CustomerAcceptanceAuthorization = {
  acceptance_token: string;
};

export type CustomerDeliveryAcceptance = {
  status: "PROCESSING";
  transfer_id: string;
};

export type WebhookEndpoint = {
  id: string;
  url: string;
  is_active: boolean;
  previous_secret_expires_at: string | null;
  created_at: string | null;
};

export type WebhookDeliveryStatus = "PENDING" | "RETRYING" | "DELIVERED" | "FAILED";

export type WebhookDelivery = {
  id: string;
  endpoint_id: string;
  event_id: string;
  agreement_id: string;
  event_type: string;
  sequence: number;
  status: WebhookDeliveryStatus;
  attempts: number;
  next_attempt_at: string | null;
  delivered_at: string | null;
  last_response_status: number | null;
  last_error: string;
  replay_count: number;
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function responseMessage(payload: unknown): string {
  if (!isRecord(payload)) {
    return "Não foi possível concluir esta ação agora.";
  }

  for (const field of ["detail", "message", "error"] as const) {
    const value = payload[field];
    if (typeof value === "string" && value.trim()) {
      return value;
    }
  }

  const errors = payload.errors;
  if (isRecord(errors)) {
    for (const fieldErrors of Object.values(errors)) {
      if (typeof fieldErrors === "string" && fieldErrors.trim()) {
        return fieldErrors;
      }

      if (Array.isArray(fieldErrors)) {
        const firstMessage = fieldErrors.find(
          (message): message is string => typeof message === "string" && message.trim().length > 0,
        );
        if (firstMessage) {
          return firstMessage;
        }
      }
    }
  }

  return "Não foi possível concluir esta ação agora.";
}

const csrfCookieName = "escrow_csrf";

function csrfToken(): string | undefined {
  const token = document.cookie
    .split(";")
    .map((entry) => entry.trim())
    .find((entry) => entry.startsWith(`${csrfCookieName}=`))
    ?.slice(csrfCookieName.length + 1);

  return token ? decodeURIComponent(token) : undefined;
}

async function ensureCsrfToken(): Promise<string> {
  const existingToken = csrfToken();
  if (existingToken) {
    return existingToken;
  }

  const response = await fetch("/api/v1/auth/csrf/", {
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  });

  if (!response.ok) {
    throw new ApiError(response.status, "Não foi possível preparar uma sessão segura.");
  }

  const token = csrfToken();
  if (!token) {
    throw new ApiError(0, "Não foi possível preparar uma sessão segura.");
  }

  return token;
}

async function request<T>(path: string, init: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
  });
  const payload: unknown =
    response.status === 204 ? undefined : await response.json().catch(() => undefined);

  if (!response.ok) {
    throw new ApiError(response.status, responseMessage(payload));
  }

  return payload as T;
}

async function publicRequest<T>(path: string): Promise<T> {
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "omit",
    headers: { Accept: "application/json" },
    method: "GET",
  });
  const contentType = response.headers.get("Content-Type") ?? "";
  const payload: unknown = contentType.includes("application/json")
    ? await response.json().catch(() => undefined)
    : undefined;

  if (!response.ok) {
    throw new ApiError(response.status, responseMessage(payload));
  }

  if (!isRecord(payload)) {
    throw new ApiError(0, "O checkout retornou uma resposta inválida.");
  }

  return payload as T;
}

async function publicPost<T>(
  path: string,
  headers: HeadersInit,
  body?: Record<string, unknown>,
): Promise<T> {
  const requestHeaders: Record<string, string> = {
    Accept: "application/json",
    ...(headers as Record<string, string>),
  };
  if (body) {
    requestHeaders["Content-Type"] = "application/json";
  }
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "omit",
    headers: requestHeaders,
    method: "POST",
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const contentType = response.headers.get("Content-Type") ?? "";
  const payload: unknown = contentType.includes("application/json")
    ? await response.json().catch(() => undefined)
    : undefined;

  if (!response.ok) {
    throw new ApiError(response.status, responseMessage(payload));
  }

  return payload as T;
}

function isPublicCheckout(value: unknown): value is PublicCheckout {
  if (!isRecord(value) || !isRecord(value.agreement)) {
    return false;
  }
  const agreement = value.agreement;
  if (!isRecord(agreement.customer)) {
    return false;
  }
  const customer = agreement.customer;
  return (
    typeof agreement.id === "string" &&
    typeof agreement.status === "string" &&
    typeof agreement.amount === "string" &&
    (agreement.currency === "BRL" || agreement.currency === "USD") &&
    typeof agreement.delivery_window_days === "number" &&
    (agreement.delivery_due_at === null || typeof agreement.delivery_due_at === "string") &&
    (agreement.inspection_deadline_at === undefined ||
      agreement.inspection_deadline_at === null ||
      typeof agreement.inspection_deadline_at === "string") &&
    (agreement.refund_reason === undefined ||
      agreement.refund_reason === null ||
      typeof agreement.refund_reason === "string") &&
    (agreement.release_reason === undefined ||
      agreement.release_reason === null ||
      typeof agreement.release_reason === "string") &&
    typeof agreement.fee_bps === "number" &&
    typeof customer.name === "string" &&
    typeof customer.email_masked === "string" &&
    typeof customer.document_masked === "string"
  );
}

function isPixChargeResponse(
  value: unknown,
): value is components["schemas"]["PublicSandboxPixChargeResponse"] {
  if (!isRecord(value) || !isRecord(value.payment)) {
    return false;
  }
  const payment = value.payment;
  return (
    typeof payment.id === "string" &&
    (payment.status === "PENDING" ||
      payment.status === "CONFIRMED" ||
      payment.status === "REJECTED") &&
    typeof payment.amount === "string" &&
    (payment.currency === "BRL" || payment.currency === "USD") &&
    typeof payment.pix_copy_paste === "string"
  );
}

async function post<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  const token = await ensureCsrfToken();
  const headers = new Headers({ Accept: "application/json", "X-CSRFToken": token });

  if (body) {
    headers.set("Content-Type", "application/json");
  }

  return request<T>(path, {
    method: "POST",
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
}

export const organizationApi = {
  register(input: RegisterInput): Promise<unknown> {
    return post("/api/v1/auth/register/", input);
  },

  login(input: Pick<RegisterInput, "email" | "password">): Promise<unknown> {
    return post("/api/v1/auth/login/", input);
  },

  logout(): Promise<unknown> {
    return post("/api/v1/auth/logout/");
  },

  recoverPassword(email: string): Promise<unknown> {
    return post("/api/v1/auth/password-recovery/", { email });
  },

  confirmPasswordRecovery(input: PasswordRecoveryConfirmationInput): Promise<unknown> {
    return post("/api/v1/auth/password-recovery/confirm/", input);
  },

  currentOrganization(): Promise<CurrentOrganization> {
    return request<CurrentOrganization>("/api/v1/organizations/current/", {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  },

  apiKeys(): Promise<ApiKeyListResponse> {
    return request<ApiKeyListResponse>("/api/v1/organizations/current/api-keys/", {
      method: "GET",
      headers: { Accept: "application/json" },
    });
  },

  createApiKey(input: CreateApiKeyInput): Promise<ApiKeySecretResponse> {
    return post<ApiKeySecretResponse>("/api/v1/organizations/current/api-keys/", input);
  },

  rotateApiKey(apiKeyId: string, overlapSeconds?: number): Promise<ApiKeySecretResponse> {
    return post<ApiKeySecretResponse>(
      `/api/v1/organizations/current/api-keys/${apiKeyId}/rotate/`,
      overlapSeconds !== undefined ? { overlap_seconds: overlapSeconds } : {},
    );
  },

  revokeApiKey(apiKeyId: string): Promise<{ api_key: OrganizationApiKey }> {
    return post<{ api_key: OrganizationApiKey }>(
      `/api/v1/organizations/current/api-keys/${apiKeyId}/revoke/`,
    );
  },

  webhookEndpoints(): Promise<{ webhook_endpoints: WebhookEndpoint[] }> {
    return request<{ webhook_endpoints: WebhookEndpoint[] }>(
      "/api/v1/organizations/current/webhooks/",
      { method: "GET", headers: { Accept: "application/json" } },
    );
  },

  createWebhookEndpoint(
    url: string,
  ): Promise<{ webhook_endpoint: WebhookEndpoint; secret: string }> {
    return post<{ webhook_endpoint: WebhookEndpoint; secret: string }>(
      "/api/v1/organizations/current/webhooks/",
      { url },
    );
  },

  rotateWebhookSecret(
    endpointId: string,
    overlapSeconds: number,
  ): Promise<{ webhook_endpoint: WebhookEndpoint; secret: string }> {
    return post<{ webhook_endpoint: WebhookEndpoint; secret: string }>(
      `/api/v1/organizations/current/webhooks/${endpointId}/rotate/`,
      { overlap_seconds: overlapSeconds },
    );
  },

  webhookDeliveries(): Promise<{ webhook_deliveries: WebhookDelivery[] }> {
    return request<{ webhook_deliveries: WebhookDelivery[] }>(
      "/api/v1/organizations/current/webhook-deliveries/",
      { method: "GET", headers: { Accept: "application/json" } },
    );
  },

  replayWebhookDelivery(deliveryId: string): Promise<{ webhook_delivery: WebhookDelivery }> {
    return post<{ webhook_delivery: WebhookDelivery }>(
      `/api/v1/organizations/current/webhook-deliveries/${deliveryId}/replay/`,
    );
  },
};

export const checkoutApi = {
  async get(token: string): Promise<PublicCheckout> {
    const payload = await publicRequest<unknown>(`/api/v1/checkout/${encodeURIComponent(token)}/`);
    if (!isPublicCheckout(payload)) {
      throw new ApiError(0, "O checkout retornou uma resposta inválida.");
    }
    return payload;
  },

  async createPixCharge(token: string, idempotencyKey: string): Promise<PixChargeResponse> {
    const payload = await publicPost<unknown>(
      `/api/v1/checkout/${encodeURIComponent(token)}/pix-charges/`,
      { "Idempotency-Key": idempotencyKey },
    );
    if (!isPixChargeResponse(payload)) {
      throw new ApiError(0, "A cobrança PIX retornou uma resposta inválida.");
    }
    return {
      status: "PROCESSING",
      pix: {
        id: payload.payment.id,
        copy_paste: payload.payment.pix_copy_paste,
        status: payload.payment.status,
      },
    };
  },

  async requestDeliveryAcceptanceOtp(token: string): Promise<CustomerOtpChallenge> {
    const payload = await publicPost<unknown>(
      `/api/v1/checkout/${encodeURIComponent(token)}/delivery-acceptance/otp/`,
      {},
      {},
    );
    if (
      !isRecord(payload) ||
      typeof payload.challenge_id !== "string" ||
      typeof payload.expires_at !== "string"
    ) {
      throw new ApiError(0, "A confirmação por e-mail retornou uma resposta inválida.");
    }
    return { challenge_id: payload.challenge_id, expires_at: payload.expires_at };
  },

  async verifyDeliveryAcceptanceOtp(
    token: string,
    challengeId: string,
    code: string,
  ): Promise<CustomerAcceptanceAuthorization> {
    const payload = await publicPost<unknown>(
      `/api/v1/checkout/${encodeURIComponent(token)}/delivery-acceptance/otp/${encodeURIComponent(challengeId)}/verify/`,
      {},
      { code },
    );
    if (!isRecord(payload) || typeof payload.acceptance_token !== "string") {
      throw new ApiError(0, "A confirmação por e-mail retornou uma resposta inválida.");
    }
    return { acceptance_token: payload.acceptance_token };
  },

  async acceptReportedDelivery(
    token: string,
    challengeId: string,
    acceptanceToken: string,
  ): Promise<CustomerDeliveryAcceptance> {
    const payload = await publicPost<unknown>(
      `/api/v1/checkout/${encodeURIComponent(token)}/delivery-acceptance/`,
      {},
      { acceptance_token: acceptanceToken, challenge_id: challengeId },
    );
    if (
      !isRecord(payload) ||
      payload.status !== "PROCESSING" ||
      typeof payload.transfer_id !== "string"
    ) {
      throw new ApiError(0, "A liberação retornou uma resposta inválida.");
    }
    return { status: "PROCESSING", transfer_id: payload.transfer_id };
  },
};
