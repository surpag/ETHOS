export type ProviderCredentialState = { providerId: string; hasApiKey: boolean };
export type UserSettingsResponse = { personality: string; defaultModelId: string; timezone: string; gitAuthorName: string; gitAuthorEmail: string; providers: ProviderCredentialState[] };
export type ProviderCredentialInput = { providerId: string; apiKey?: string };
export type SaveUserSettingsRequest = { personality: string; defaultModelId: string; timezone?: string; gitAuthorName?: string; gitAuthorEmail?: string; providers: ProviderCredentialInput[] };

