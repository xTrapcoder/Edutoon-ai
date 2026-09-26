import { SignIn } from '@clerk/nextjs';
import type { Metadata } from 'next';
import { AuthLayout } from '@/components/brand/auth-layout';

export const metadata: Metadata = { title: 'Sign in' };

export default function SignInPage() {
  return (
    <AuthLayout>
      <SignIn appearance={{ variables: { colorPrimary: '#4f46e5', borderRadius: '8px' } }} />
    </AuthLayout>
  );
}
