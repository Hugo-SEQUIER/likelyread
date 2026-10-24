import { test } from '@e2e-dev/web';
import { expect } from 'e2e';
import { REVIEWER } from '../support/cloud-fakes.mjs';

// The reviewer sign-in (server/review.py): the Claude and ChatGPT directory reviewers
// sign in to one test account with a password, and find it on Pro with sample emails.

test('a reviewer signs in with the test account and finds sample emails', { retries: 0, requires: ['browser'] }, async ({ app, screen }) => {
  await app.open('/');
  await screen.getByRole('link', 'App reviewers: sign in with the test account').tap();
  await expect(screen.getByRole('heading', 'Sign in')).toBeVisible();
  await expect(screen.getByRole('link', 'Continue with Google')).toBeVisible();

  await screen.getByLabel('Email').fill(REVIEWER.email);
  await screen.getByLabel('Password').fill('not the password at all');
  await screen.getByRole('button', 'Sign in').tap();
  await expect(screen.getByText('Wrong email or password.')).toBeVisible();

  await screen.getByLabel('Email').fill(REVIEWER.email);
  await screen.getByLabel('Password').fill(REVIEWER.password);
  await screen.getByRole('button', 'Sign in').tap();
  // On Pro, with opens, Apple prefetches and a self-view to look at.
  await expect(screen.getByText('62% open rate', { exact: false })).toBeVisible();
  await screen.getByRole('link', 'Emails').tap();
  await expect(screen.getByText('Pilot pricing for your team')).toBeVisible();
  // An email only its sender has viewed is not called "Not opened".
  await expect(screen.getByText('Seen by you only')).toBeVisible();
});
