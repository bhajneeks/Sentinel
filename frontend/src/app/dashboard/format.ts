export const maskParticipant = (raw: string | null | undefined): string => {
  if (!raw) return "—";
  if (raw.includes("@")) {
    const at = raw.indexOf("@");
    const local = raw.slice(0, at);
    const domain = raw.slice(at);
    if (local.length <= 1) return `•${domain}`;
    return `${local[0]}${"•".repeat(Math.max(3, local.length - 1))}${domain}`;
  }
  const digits = raw.replace(/\D/g, "");
  const tail = digits.length >= 4 ? digits.slice(-4) : raw.slice(-4);
  return `•••• ${tail}`;
};

export const maskedInitial = (raw: string | null | undefined): string => {
  if (!raw) return "?";
  if (raw.includes("@")) {
    return (raw[0] ?? "?").toUpperCase();
  }
  const digits = raw.replace(/\D/g, "");
  const tail = digits.length >= 4 ? digits.slice(-4) : digits;
  return tail[0] ?? "#";
};
