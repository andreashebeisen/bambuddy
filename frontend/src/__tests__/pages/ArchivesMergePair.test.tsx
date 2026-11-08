/**
 * Merging a print with no file (a cloud print) into its uploaded file, from
 * the archives selection toolbar.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { ArchivesPage } from '../../pages/ArchivesPage';
import { server } from '../mocks/server';
import { setAuthToken } from '../../api/client';

const base = {
  printer_id: 1,
  printer_name: 'X1 Carbon',
  print_time_seconds: 3600,
  filament_used_grams: 15.5,
  status: 'completed',
  thumbnail_path: null,
  notes: null,
  rating: null,
  project_id: null,
  project_name: null,
  project_color: null,
  print_count: 1,
  tags: '',
  created_at: '2026-11-01T09:00:00Z',
  updated_at: '2026-11-01T11:00:00Z',
  has_f3d: false,
};
const cloudPrint = { ...base, id: 10, filename: 'part.gcode.3mf', print_name: 'Cloud print', file_path: '' };
const uploaded = { ...base, id: 11, filename: 'part.gcode.3mf', print_name: 'Uploaded file', file_path: 'archive/u/part.gcode.3mf' };
const another = { ...base, id: 12, filename: 'other.gcode.3mf', print_name: 'Another file', file_path: 'archive/o/other.gcode.3mf' };

function serve(archives: object[]) {
  server.use(
    http.get('/api/v1/archives/', () => HttpResponse.json(archives)),
    http.get('/api/v1/archives/stats', () =>
      HttpResponse.json({ total_archives: archives.length, successful_prints: 0, failed_prints: 0, total_print_time_hours: 0, total_filament_grams: 0, total_cost: 0 }),
    ),
    http.get('/api/v1/printers/', () => HttpResponse.json([{ id: 1, name: 'X1 Carbon' }])),
    http.get('/api/v1/projects/', () => HttpResponse.json([])),
    http.get('/api/v1/archives/tags', () => HttpResponse.json([])),
    http.get('/api/v1/archives/merge-count', () => HttpResponse.json({ groups: 0 })),
  );
}

async function selectAll() {
  fireEvent.click(await screen.findByRole('button', { name: /^Select$/ }));
  fireEvent.click(await screen.findByRole('button', { name: /Select All/i }));
}

describe('ArchivesPage merge pair', () => {
  beforeEach(() => {
    setAuthToken(null);
  });

  it('merges a print without a file into the archive with the file', async () => {
    serve([cloudPrint, uploaded]);
    let body: unknown = null;
    server.use(
      http.post('/api/v1/archives/merge-pair', async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ survivor_id: 11, merged_id: 10 });
      }),
    );
    render(<ArchivesPage />);
    await screen.findByText('Cloud print');
    await selectAll();

    const merge = await screen.findByRole('button', { name: /^Merge$/ });
    expect(merge).toBeEnabled();
    fireEvent.click(merge);
    fireEvent.click(await screen.findByRole('button', { name: /Yes, merge/i }));

    await waitFor(() => expect(body).toEqual({ archive_ids: [11, 10] }));
    expect(await screen.findByText('Merged the print into its file.')).toBeInTheDocument();
  });

  it('is disabled for two archives that both hold a file', async () => {
    serve([uploaded, another]);
    render(<ArchivesPage />);
    await screen.findByText('Uploaded file');
    await selectAll();

    expect(await screen.findByRole('button', { name: /^Merge$/ })).toBeDisabled();
  });

  it('shows why the server left them out', async () => {
    serve([cloudPrint, uploaded]);
    server.use(
      http.post('/api/v1/archives/merge-pair', () =>
        HttpResponse.json({ detail: { reason: 'busy' } }, { status: 409 }),
      ),
    );
    render(<ArchivesPage />);
    await screen.findByText('Cloud print');
    await selectAll();
    fireEvent.click(await screen.findByRole('button', { name: /^Merge$/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Yes, merge/i }));

    expect(await screen.findByText(/In use: printing, queued or just printed/)).toBeInTheDocument();
  });
});
