import { setupServer } from 'msw/node';

/** The network for every test: nothing is handled until a test says what the API answers. */
export const server = setupServer();

export const API = 'http://localhost:8000';
