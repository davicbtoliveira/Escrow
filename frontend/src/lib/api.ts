import type { components } from "./generated/openapi";

export type ScheduledRelease = {
  id: string;
  amount_minor: number;
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
    typeof agreement.fee_bps === "number" &&
    typeof customer.name === "string" &&
    typeof customer.email_masked === "string" &&
    typeof customer.document_masked === "string"
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
};

export const checkoutApi = {
  async get(token: string): Promise<PublicCheckout> {
    const payload = await publicRequest<unknown>(`/api/v1/checkout/${encodeURIComponent(token)}/`);
    if (!isPublicCheckout(payload)) {
      throw new ApiError(0, "O checkout retornou uma resposta inválida.");
    }
    return payload;
  },
};
