import chartNotice from "./licenses/lightweight-charts/NOTICE?raw";

/** A third-party library whose license asks Hokma to show its notice. The build copies the same files to dist/licenses/. */
export interface OpenSourceNotice {
  name: string;
  license: string;
  licenseUrl: string;
  /** The NOTICE text, exactly as the library publishes it. */
  notice: string;
}

export const NOTICES: readonly OpenSourceNotice[] = [
  {
    name: "lightweight-charts",
    license: "Apache License 2.0",
    licenseUrl: "https://www.apache.org/licenses/LICENSE-2.0",
    notice: chartNotice,
  },
];
